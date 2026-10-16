"""Opt-in, capped real-book session audit on a disposable SQLite snapshot; source data is read-only."""
import argparse
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.answer_bank import valid_answer
from app.config import Settings
from app.ingest.manifest import load_manifest, validated_book_id
from app.main import create_app


CANARY = "UNREAD_MEMORY_CANARY_74D9BE3F"
# Frozen before observing prepared questions. Ground truth comes from this supplied edition's
# raw completed chapters, not generated memory. Four independent answer probes per checkpoint.
CASES = {
    2: [
        ("Who looked after Dmitri when Fyodor neglected him as a small child?", "Grigory took the three-year-old Mitya into his care."),
        ("How was Pyotr Miusov related to Dmitri's mother?", "He was a cousin of Mitya's mother."),
        ("Where did the young Dmitri live while Grigory cared for him?", "In the servant's cottage, for almost a year."),
        ("Did Fyodor abandon Dmitri because he hated him?", "The narrator says not from malice or matrimonial grievances, but because he forgot him."),
    ],
    5: [
        ("Who is Alyosha's spiritual elder?", "Zossima, whose cell Alyosha lived in and whom he waited upon."),
        ("How old is the elder Zossima?", "Sixty-five."),
        ("Where did Zossima serve as an officer in his youth?", "In the Caucasus."),
        ("Was Alyosha forbidden to leave the monastery during the day?", "No; he was bound by no obligation and could go where he pleased, absent for whole days."),
    ],
    12: [
        ("What does Zossima tell Alyosha to do after the elder dies?", "Leave the monastery for good and perform great service in the world."),
        ("Does Zossima say Alyosha will have to marry?", "Yes, he says Alyosha will have to take a wife."),
        ("Which brothers does Zossima tell Alyosha to stay near?", "Both brothers, not just one."),
        ("Does Zossima tell Alyosha to remain in the monastery forever?", "No; he tells him to leave for good when God calls the elder."),
    ],
    24: [
        ("Who wrote the love letter Alyosha reads that evening?", "Lise, Madame Hohlakov's young daughter."),
        ("How long does Lise say she has loved Alyosha?", "Since childhood, from their Moscow days."),
        ("What condition does Lise place on their future life together?", "Alyosha must leave the monastery; they will also wait for the legal age."),
        ("Does Alyosha react to Lise's letter with contempt?", "No; he laughs softly and happily, prays, and falls into peaceful sleep."),
    ],
    48: [
        ("How much money does Dmitri hope to borrow from Madame Hohlakov?", "Three thousand roubles."),
        ("What does Madame Hohlakov suggest Dmitri do instead of lending him the money?", "Go to the gold-mines."),
        ("What object does Dmitri grab from Fenya's table, and where does he put it?", "The small brass pestle from the mortar; he puts it in his side-pocket."),
        ("Does Madame Hohlakov actually give Dmitri the three thousand roubles?", "No; she discusses the gold-mines rather than giving him the loan."),
    ],
    72: [
        ("To whom is the sealed note Lise gives Alyosha addressed?", "Ivan Fyodorovitch Karamazov."),
        ("When does Lise insist Alyosha deliver the sealed note?", "Today, at once."),
        ("How does Lise know what her mother has been telling Alyosha?", "She says she has been listening."),
        ("Does Alyosha return to Madame Hohlakov after leaving Lise?", "No; he puts the note in his pocket and goes straight downstairs, forgetting her."),
    ],
}
FUTURE_QUESTION = "To whom does Lise ask Alyosha to deliver the sealed note she presses into his hand?"


def ro(path):
    return sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True)


def fingerprint(source, bid):
    source = Path(source).resolve()
    with closing(ro(source/"catalog.db")) as db:
        state = db.execute("SELECT * FROM reading_state WHERE book_id=?", (bid,)).fetchone()
        costs = db.execute("SELECT COUNT(*),COALESCE(SUM(usd),0) FROM cost_ledger WHERE book_id=?", (bid,)).fetchone()
        pending = db.execute("SELECT COUNT(*) FROM cost_reservations WHERE book_id=?", (bid,)).fetchone()[0]
    files = {}
    for name in ("atoms.json", "source.epub", "memory.db", "memory.db-wal"):
        path = source/"books"/bid/name
        if path.exists():
            with path.open("rb") as stream:
                files[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"reading_state": state, "costs": costs, "pending": pending, "files": files}


def clone_book(source, destination, bid):
    """Never open the user's databases writable or copy an open SQLite file without its WAL."""
    validated_book_id(bid)
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.is_relative_to(source) or source.is_relative_to(destination) or destination.exists():
        raise ValueError("audit snapshot must be new and separate from source data")
    for relative in ("catalog.db", f"books/{bid}/memory.db", f"books/{bid}/atoms.json", f"books/{bid}/source.epub"):
        path = source/relative
        if path.is_symlink() or not path.resolve().is_relative_to(source) or not path.is_file():
            raise ValueError("source book path escaped the data directory or is missing")
    before = fingerprint(source, bid)
    if before["reading_state"] is None or before["pending"]:
        raise ValueError("source book is unknown or has in-flight spend")
    target = destination/"books"/bid
    target.mkdir(parents=True)
    for relative in ("catalog.db", f"books/{bid}/memory.db"):
        with closing(ro(source/relative)) as origin, closing(sqlite3.connect(destination/relative)) as snapshot:
            origin.backup(snapshot)
    with closing(sqlite3.connect(destination/"catalog.db")) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("DELETE FROM books WHERE book_id<>?", (bid,))
        # Replay a new partial-reading session with the original future caches still present.
        # This is a snapshot fixture seed, not a reset of the user's original reading authority.
        db.execute("UPDATE reading_state SET bookmark=0,cfi=NULL WHERE book_id=?", (bid,))
        db.commit()
    for name in ("atoms.json", "source.epub"):
        shutil.copyfile(source/"books"/bid/name, target/name)
    for name in ("reviewed-answers-v2.json", "reviewed-recaps-v1.json"):
        path = source/name
        if path.is_file() and not path.is_symlink():
            shutil.copyfile(path, destination/name)
    if fingerprint(source, bid) != before:
        raise RuntimeError("source changed while creating audit snapshot")
    return before


def install_observers(client, report, phase):
    """Observe below complete(), preserving its method identity and all production token caps."""
    method = {"stub": "_complete_stub", "anthropic": "_complete_anthropic"}.get(client.provider, "_complete_openai")
    original = getattr(client, method)
    def observed(system, user, tier, schema, max_output_tokens):
        row = {**phase, "provider": "generative", "schema": getattr(schema, "__name__", None),
               "output_cap": max_output_tokens, "unread_canary": CANARY in system+user}
        started = time.perf_counter()
        try:
            if row["unread_canary"]:
                raise RuntimeError("unread memory reached a provider input")
            result = original(system, user, tier, schema, max_output_tokens)
            if row["schema"] == "PreparedQuestions":
                row["planned_questions"] = result[0].get("questions", [])
            elif row["schema"] == "PreparationReview":
                row["accepted_question_ids"] = [a["question_id"] for a in result[0].get("audits", [])
                                                if a.get("safe_to_prepare")]
            return result
        except Exception as error:
            row["error"] = type(error).__name__
            raise
        finally:
            row["seconds"] = time.perf_counter()-started
            report["provider_calls"].append(row)
    if client.provider == "openai-compatible" and getattr(client, "_is_native_openai", False):
        # Observe the actual wire call. Wrapping a bound _complete_openai method would bind
        # copied foreground-review clients back to the original client's reasoning/tier.
        def observe_wire(original_wire):
            def wire(**kwargs):
                schema = kwargs.get("response_format")
                row = {**phase, "provider": "generative", "schema": getattr(schema, "__name__", None),
                    "output_cap": kwargs.get("max_completion_tokens"),
                    "reasoning_effort": kwargs.get("reasoning_effort"),
                    "service_tier": kwargs.get("service_tier"),
                    "unread_canary": CANARY in json.dumps(kwargs.get("messages", []))}
                started = time.perf_counter()
                try:
                    if row["unread_canary"]:
                        raise RuntimeError("unread memory reached a provider input")
                    result = original_wire(**kwargs)
                    parsed = getattr(result.choices[0].message, "parsed", None)
                    value = parsed.model_dump(mode="json") if parsed is not None else {}
                    if row["schema"] == "PreparedQuestions":
                        row["planned_questions"] = value.get("questions", [])
                    elif row["schema"] == "PreparationReview":
                        row["accepted_question_ids"] = [a["question_id"] for a in value.get("audits", [])
                                                        if a.get("safe_to_prepare")]
                    elif row["schema"] == "Extraction":
                        row["entity_names"] = [{"name": e["canonical_name"], "aliases": e.get("aliases", [])}
                                               for e in value.get("entities", [])]
                    elif row["schema"] == "ExtractionVerdict":
                        row["review_rejections"] = {k: value.get(k) for k in
                                                   ("references_future", "unsupported_claims")}
                        from app.ingest.extraction.validation import _validate_field_evidence
                        data = json.loads(kwargs["messages"][-1]["content"].split("(untrusted JSON data):\n", 1)[1])
                        try:
                            _validate_field_evidence(parsed, data["fields"], data["source_passages"],
                                                     data["prior_visible_identities"])
                        except ValueError as error:
                            row["audit_integrity_error"] = str(error)
                            row["requested_fields"] = [f["id"] for f in data["fields"]]
                            row["returned_fields"] = [f["field_id"] for f in value["field_audits"]]
                            expected = {f["id"]: [s["id"] for s in f["segments"]] for f in data["fields"]}
                            row["segment_mismatches"] = [
                                {"field": f["field_id"], "expected": expected.get(f["field_id"]),
                                 "returned": [s["segment_id"] for s in f["assertions"]]}
                                for f in value["field_audits"]
                                if [s["segment_id"] for s in f["assertions"]] != expected.get(f["field_id"])]
                    elif row["schema"] in {"ReuseVerdict", "PreparedResponseVerdict"}:
                        row["review"] = value
                    return result
                except Exception as error:
                    row["error"] = type(error).__name__
                    raise
                finally:
                    row["seconds"] = time.perf_counter()-started
                    report["provider_calls"].append(row)
            return wire
        completions = client._oai.chat.completions
        completions.parse = observe_wire(completions.parse)
        completions.create = observe_wire(completions.create)
    else:
        setattr(client, method, observed)
    original_embed = client.embed
    def observed_embed(texts):
        row = {**phase, "provider": "embedding", "unread_canary": CANARY in json.dumps(texts)}
        started = time.perf_counter()
        try:
            if row["unread_canary"]:
                raise RuntimeError("unread memory reached a provider input")
            return original_embed(texts)
        except Exception as error:
            row["error"] = type(error).__name__
            raise
        finally:
            row["seconds"] = time.perf_counter()-started
            report["provider_calls"].append(row)
    client.embed = observed_embed
    if getattr(client, "jev", None) is not None:
        original_jev = client.jev.evaluate
        def observed_jev(state, questions, **kwargs):
            row = {**phase, "provider": "jev", "unread_canary": CANARY in json.dumps([state, questions])}
            started = time.perf_counter()
            try:
                if row["unread_canary"]:
                    raise RuntimeError("unread memory reached a provider input")
                result = original_jev(state, questions, **kwargs)
                if any(k.startswith("coverage") for k in questions):
                    row["reuse_decisions"] = result[0]
                return result
            except Exception as error:
                row["error"] = type(error).__name__
                raise
            finally:
                row["seconds"] = time.perf_counter()-started
                report["provider_calls"].append(row)
        client.jev.evaluate = observed_jev


def restore_rows(bank, rows):
    # Independent probes must not count an earlier probe's fresh answer as a preparation hit.
    with bank.lock:
        bank.rows = deepcopy(rows)
        bank._save()


def latency(rows):
    values = sorted(r["seconds"] for r in rows)
    if not values:
        return {"count": 0, "median": None, "max": None, "under_1_5": 0}
    return {"count": len(values), "median": statistics.median(values), "max": max(values),
            "under_1_5": sum(v <= 1.5 for v in values)}


def aggregate(report):
    probes = [p for stage in report["checkpoints"] for p in stage["probes"]]
    supported = [p for p in probes if p["expected"] == "answer"]
    future = [p for p in probes if p["expected"] == "refuse"]
    return {"independent_questions": len(supported), "preparation_hits": sum(p.get("cached", False) for p in supported),
            "answered": sum(p.get("answered", False) for p in supported),
            "answered_under_1_5_seconds": sum(p.get("answered", False) and p["seconds"] <= 1.5 for p in supported),
            "future_questions": len(future), "future_answers": sum(p.get("answered", False) for p in future),
            "future_refusals": sum(p.get("status") == 200 and p.get("insufficient_evidence") is True
                                   and not p.get("claims") for p in future),
            "api_errors": sum(p.get("status") != 200 for p in probes),
            "invalid_answer_shapes": sum(p.get("invalid_answer_shape", False) for p in probes),
            "future_citations": sum(p.get("future_citation", False) for p in probes),
            "provider_canary_leaks": sum(p["unread_canary"] for p in report["provider_calls"]),
            "all_independent_latency": latency(supported),
            "all_request_latency": latency(probes),
            "successful_answer_latency": latency([p for p in supported if p.get("answered")]),
            "preparation_hit_latency": latency([p for p in supported if p.get("cached")]),
            "preparation_miss_latency": latency([p for p in supported if not p.get("cached")]),
            "page_save_latency": latency([p for s in report["checkpoints"] for p in s["page_saves"]]),
            "semantic_accuracy": "Requires manual comparison with source references; refusals, status codes and token matches do not prove accuracy."}


def run_session(settings, bid, output, *, cases=CASES):
    report = {"scope": "One existing real novel, existing legacy extraction; no re-extraction or quality-oracle assumptions.",
              "method": "Questions frozen before planning. Restore the pre-probe answer bank for each independent probe; exact suggested-question controls are excluded from hit-rate denominators.",
              "provider_calls": [], "checkpoints": []}
    phase = {"kind": "startup", "frontier": 0}
    def save():
        report["summary"] = aggregate(report)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    app = create_app(settings)
    with TestClient(app) as api:
        state = app.state
        install_observers(state.reader_client, report, phase)
        manifest = load_manifest(settings.data_dir, bid)
        total = len(manifest["atoms"])
        if max(cases) >= total:
            raise ValueError("real-book audit needs unread chapters beyond the last checkpoint")
        with state.store.book(bid) as mem:
            a = mem.add_entity(CANARY+"_person", "character", total)
            b = mem.add_entity(CANARY+"_other", "character", total)
            mem.add_alias(a, CANARY+"_alias", total)
            mem.add_state(a, total, {"note": CANARY+"_state"})
            mem.add_edge(a, b, "family", CANARY+"_tie", total)
            mem.add_event(CANARY+"_event", total, 9999, participants=[(a, "actor")])
            mem.reextract_summary(manifest["atoms"][-1]["key"], total, CANARY+"_summary")
        baseline = state.catalog.total_cost(bid)
        report["historical_usd"] = baseline
        report["limits"] = {"book_usd": settings.cost_max_usd_per_book,
            "book_input_tokens": settings.cost_max_input_tokens_per_book,
            "book_output_tokens": settings.cost_max_output_tokens_per_book,
            "preparation_usd": settings.reader_prepare_max_usd_per_book}
        historical_count = len(state.catalog.get_costs(bid))
        epoch = state.catalog.get_state(bid)["position_epoch"]
        for frontier, questions in cases.items():
            phase.update(kind="preparation", frontier=frontier)
            stage = {"frontier": frontier, "page_saves": [], "probes": []}
            report["checkpoints"].append(stage)
            offset = sum(a["char_len"] for a in manifest["atoms"][:frontier])
            response = api.put(f"/api/books/{bid}/position", json={
                "cfi": f"audit-{frontier}", "offset": offset, "position_epoch": epoch})
            if response.status_code != 200:
                raise RuntimeError("snapshot position report failed")
            stage["preparation_started"] = time.perf_counter()
            for index in range(8):
                started = time.perf_counter()
                response = api.put(f"/api/books/{bid}/position", json={
                    "cfi": f"audit-{frontier}-{index}", "offset": max(0, offset-index), "position_epoch": epoch})
                stage["page_saves"].append({"seconds": time.perf_counter()-started, "status": response.status_code})
                if response.status_code != 200 or response.json()["bookmark"] != frontier:
                    raise RuntimeError("backward page changed the completed frontier")
            api.portal.call(state.recap_warmer.queue.join)
            stage["preparation_seconds"] = time.perf_counter()-stage.pop("preparation_started")
            offered = api.get(f"/api/books/{bid}/prepared-questions").json()
            stage["prepared_questions"] = offered["questions"]
            stage["ingest"] = api.get(f"/api/books/{bid}/ingest").json()
            bank = state.reader_client.answers
            prepared_rows = deepcopy(bank.rows)
            print(f"Real-book frontier {frontier}: {len(offered['questions'])} prepared; probing held-out questions", flush=True)
            phase.update(kind="probe")
            rows = [(q, reference, "answer") for q, reference in questions]
            future = FUTURE_QUESTION if frontier < 72 else "What does Alyosha say in the speech at the stone after Ilyusha's funeral?"
            rows.append((future, "The specified scene is in an unread chapter; withhold the answer.", "refuse"))
            for question, reference, expected in rows:
                restore_rows(bank, prepared_rows)
                before_calls = len(report["provider_calls"])
                started = time.perf_counter()
                response = api.post(f"/api/books/{bid}/ask", json={"question": question, "bookmark": frontier})
                elapsed = time.perf_counter()-started
                value = response.json()
                row = {"question": question, "reference": reference, "expected": expected,
                    "seconds": elapsed, "status": response.status_code, "cached": value.get("cached", False),
                    "answered": response.status_code == 200 and bool(value.get("claims")),
                    "as_of_chapter": value.get("as_of_chapter"), "claims": value.get("claims", []),
                    "insufficient_evidence": value.get("insufficient_evidence"),
                    "citations": value.get("citations", []),
                    "provider_calls": len(report["provider_calls"])-before_calls,
                    "future_citation": any(c["ordinal"] > frontier for c in value.get("citations", []))}
                if row["answered"] and not valid_answer(value):
                    row["invalid_answer_shape"] = True
                stage["probes"].append(row)
                save()
            restore_rows(bank, prepared_rows)
            phase.update(kind="control")
            if offered["questions"]:
                before_calls = len(report["provider_calls"])
                started = time.perf_counter()
                value = api.post(f"/api/books/{bid}/ask", json={"question": offered["questions"][0]}).json()
                stage["suggestion_control"] = {"seconds": time.perf_counter()-started,
                    "cached": value.get("cached", False), "provider_calls": len(report["provider_calls"])-before_calls}
            before_calls = len(report["provider_calls"])
            started = time.perf_counter()
            recap = api.get(f"/api/books/{bid}/catch-me-up")
            stage["recap"] = {"seconds": time.perf_counter()-started, "status": recap.status_code,
                "provider_calls": len(report["provider_calls"])-before_calls, "value": recap.json()}
            stage["new_usd"] = state.catalog.total_cost(bid)-baseline
            save()
        phase.update(kind="reset")
        before = len(report["provider_calls"])
        response = api.post(f"/api/books/{bid}/position/reset", json={"position_epoch": epoch})
        if response.status_code != 200:
            raise RuntimeError("snapshot reset failed")
        after = api.post(f"/api/books/{bid}/ask", json={"question": "Who is Alyosha?", "bookmark": 999}).json()
        report["reset_safe"] = after.get("as_of_chapter") == 0 and not after.get("claims")
        report["reset_provider_calls"] = len(report["provider_calls"])-before
        report["new_usd"] = state.catalog.total_cost(bid)-baseline
        report["pending_reservations"] = len(state.catalog.get_cost_reservations(bid))
        new_costs = state.catalog.get_costs(bid)[historical_count:]
        report["preparation_usd"] = sum(r["usd"] for r in new_costs
            if r["phase"].startswith("reader-prepare:"))
        report["failed_paid_phases"] = [r["phase"] for r in new_costs
            if r["phase"].endswith(("failed-reserved", "provider-limit-violation"))]
        save()
    restarted = create_app(settings)
    with TestClient(restarted) as api:
        before = restarted.state.catalog.total_cost(bid)
        api.portal.call(restarted.state.recap_warmer.queue.join)
        value = api.get(f"/api/books/{bid}/prepared-questions").json()
        report["reset_restart_safe"] = value["as_of_chapter"] == 0 and not value["questions"]
        report["restart_new_usd"] = restarted.state.catalog.total_cost(bid)-before
    save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--source-data-dir", type=Path, required=True)
    parser.add_argument("--book-id", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checkpoints", type=int, nargs="+", choices=CASES, default=list(CASES))
    parser.add_argument("--preserve-token-limits", action="store_true",
                        help="Retain the configured lifetime token ceilings to reproduce foreground headroom")
    parser.add_argument("--max-new-usd", type=float, default=.50)
    args = parser.parse_args()
    if not args.run:
        parser.error("--run is required for this capped paid audit")
    if not 0 < args.max_new_usd <= .50:
        parser.error("--max-new-usd must be positive and at most 0.50")
    if args.output.resolve().is_relative_to(args.source_data_dir.resolve()):
        parser.error("audit output must be outside the original data directory")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    original = None
    with tempfile.TemporaryDirectory(prefix="real-book-audit-", dir=args.output.parent) as scratch:
        data = Path(scratch)/"data"
        original = clone_book(args.source_data_dir, data, args.book_id)
        with closing(ro(data/"catalog.db")) as db:
            spent, used_input, used_output = db.execute(
                "SELECT COALESCE(SUM(usd),0),COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0) "
                "FROM cost_ledger WHERE book_id=?", (args.book_id,)).fetchone()
        manifest = load_manifest(str(data), args.book_id)
        if "Fyodor Pavlovitch Karamazov" not in manifest["atoms"][0]["title"]:
            parser.error("frozen cases require the supplied Brothers Karamazov edition")
        configured = Settings(_env_file=str(args.env_file.resolve()))
        settings = Settings(_env_file=str(args.env_file.resolve()), data_dir=str(data),
            frontend_dist_dir=None, trusted_hosts="testserver", deployment_mode="local",
            openai_base_url="https://api.openai.com/v1", jev_enabled=True, jev_windows_credential=True,
            jev_selection_enabled=True, jev_evidence_enabled=True, jev_partial_evidence_enabled=True,
            jev_recap_enabled=False, reader_fast_mode=False, reader_answer_cache=True,
            reader_prepare_enabled=True, reader_reuse_enabled=True, recap_prewarm=True,
            recap_disk_cache=True, reader_prepare_delay_seconds=.05,
            reader_prepare_max_usd_per_book=.25, cost_max_usd_per_book=min(1.0, spent+args.max_new_usd),
            # The replay retains the entire original book's ledger. Add a separately bounded
            # test allowance instead of mistaking historical token exhaustion for hit-rate data.
            # This applies only to the disposable snapshot; original production limits stay intact.
            cost_max_input_tokens_per_book=(configured.cost_max_input_tokens_per_book
                if args.preserve_token_limits else used_input+2_000_000),
            cost_max_output_tokens_per_book=(configured.cost_max_output_tokens_per_book
                if args.preserve_token_limits else used_output+200_000))
        report = run_session(settings, args.book_id, args.output,
            cases={f: CASES[f] for f in sorted(set(args.checkpoints))})
    report["original_preserved"] = fingerprint(args.source_data_dir, args.book_id) == original
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report["summary"]), flush=True)
    return int(not report["original_preserved"] or not report["reset_safe"]
        or report["summary"]["future_answers"] or report["summary"]["future_citations"]
        or report["summary"]["provider_canary_leaks"] or report["summary"]["api_errors"]
        or report["summary"]["invalid_answer_shapes"]
        or report["summary"]["future_refusals"] != report["summary"]["future_questions"])


if __name__ == "__main__":
    raise SystemExit(main())
