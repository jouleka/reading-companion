"""Opt-in bounded audit of the production prepared-answer selector and own-citation review."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.answer_reuse import (PreparedResponseVerdict, SELECT_REVIEW_SYSTEM,
    reviewed_response_request, reuse_review_client, validated_response_selection)
from app.config import Settings
from app.cost import budgeted_completion
from app.deps import build_state
from app.llm.jev import JevUnavailable
from jev_reuse_audit import GROUPS, HELDOUT


def candidate(question, answer, source=None):
    return {"reviewed_question": question, "payload": {"as_of_chapter": 2,
        "insufficient_evidence": False, "claims": [{"text": answer, "citation_ids": [1]}],
        "citations": [{"id": 1, "ordinal": 2, "chapter_key": "c2", "title": "Chapter two",
                       "href": "c2.xhtml", "excerpt": source or answer}]}}


def cases():
    for group in (GROUPS, HELDOUT):
        for original, answer, positives, negatives in group:
            rows = [candidate(original, answer)]
            rows += [candidate(q, a) for q, a, *_ in group if q != original]
            for expected, questions in ((True, positives), (False, negatives)):
                for question in questions:
                    yield question, rows, expected
    # These poisoned candidates must fail even though their original question looks relevant.
    for question, answer, source in [
        ("How is Rafe related to Mina?", "Rafe is Mina's maternal cousin.", "Rafe is a cousin of Mina's mother."),
        ("Who stole the ring?", "Mina stole the ring.", "Rafe suspected Mina stole the ring, but had no proof."),
        ("How many coins remain?", "Three coins remain.", "Mina spent three coins; she did not count the rest."),
        ("Where did Rafe hide the key?", "Rafe hid the key under the bed.", "Mina hid the key under the bed. Rafe was outside."),
        ("What did Mina tell Rafe to do?", "Mina told Rafe to leave the tower.", "Mina said, 'Leave the tower; I send you to bring medicine to the village before noon.'"),
        ("Why did Mina leave?", "Mina left because she feared Rafe.", "Mina left. Rafe arrived later. Her reason was not stated."),
        ("Did Rafe promise to return?", "Rafe promised to return.", "Rafe refused to promise to return."),
        ("Who is the prisoner?", "The prisoner is Rafe.", "The prisoner looked out of the window. Mina spoke to Rafe downstairs."),
        ("Where did Bea meet Aldric?", "Berenice met Aldric at the forge.", "Berenice met Aldric at the forge. Bea stayed home."),
        ("Did Nell leave because of fear?", "Nell left because she needed money.", "Nell left because she needed money, not because of fear."),
    ]:
        yield question, [candidate(question, answer, source)], False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.run:
        parser.error("--run is required; spending is capped at $0.05")
    report = {"scope": "Frozen synthetic selection, omission and poisoned-own-evidence probes; not real-book performance.", "cases": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="reuse-quality-", dir=args.output.parent) as temporary:
        settings = Settings(_env_file=str(args.env_file), data_dir=temporary,
            jev_enabled=False, reader_prepare_enabled=False, recap_prewarm=False,
            cost_max_usd_per_book=.05)
        state = build_state(settings)
        catalog = state["catalog"]
        catalog.add_book("synthetic-audit", title="Synthetic reuse audit")
        client = reuse_review_client(state["reader_client"], settings)
        try:
            for question, candidates, expected in cases():
                started = time.perf_counter()
                row = {"question": question, "expected": expected, "accepted": False}
                try:
                    user, retained = reviewed_response_request(question, candidates, frontier=2, scene=[], settings=settings)
                    result = budgeted_completion(catalog, settings, client, "synthetic-audit",
                        phase="reuse-audit", system=SELECT_REVIEW_SYSTEM, user=user,
                        tier="cheap", schema=PreparedResponseVerdict)
                    row["verdict"] = result.value
                    selected = validated_response_selection(result.value, retained, question=question)
                    row.update(accepted=True, answer=selected["payload"]["claims"])
                except JevUnavailable:
                    pass
                except Exception as error:
                    row["error"] = type(error).__name__
                row["seconds"] = time.perf_counter()-started
                report["cases"].append(row)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                if len(report["cases"]) % 10 == 0:
                    print(f"{len(report['cases'])} cases; false reuse: {sum(c['accepted'] and not c['expected'] for c in report['cases'])}", flush=True)
            report["summary"] = {"cases": len(report["cases"]),
                "false_reuses": sum(c["accepted"] and not c["expected"] for c in report["cases"]),
                "positive_reused": sum(c["accepted"] and c["expected"] for c in report["cases"]),
                "positive_cases": sum(c["expected"] for c in report["cases"]),
                "median_seconds": statistics.median(c["seconds"] for c in report["cases"]),
                "usd": catalog.total_cost("synthetic-audit"),
                "pending": len(catalog.get_cost_reservations("synthetic-audit"))}
        finally:
            for name in ("client", "reader_client"):
                for attr in ("_oai", "_anthropic", "_embed_oai"):
                    transport = getattr(state[name], attr, None)
                    if transport is not None:
                        transport.close()
            state["store"].close()
            catalog.close()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"]))
    return int(report["summary"]["false_reuses"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
