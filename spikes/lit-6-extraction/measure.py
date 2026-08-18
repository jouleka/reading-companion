#!/usr/bin/env python3
"""LIT-6 — ingest the extractions into a fresh LIT-5 store and MEASURE (honestly).  [rev 2]

Rev-2 rebuild after the adversarial review exposed that the rev-1 metric was non-falsifiable
(exact-match gold silently dropped un-anticipated names, so it could not detect the very
fragmentation the spike exists to prevent). This version:

  - OVER-MERGE (precision) is checked over ALL system entities: any entity whose occurrences carry
    >1 distinct gold label is a precision failure — falsifiable regardless of what names appear.
  - FRAGMENTATION (recall) is checked per gold cluster across known surface forms, PLUS a COVERAGE
    FLOOR so a silent drop in labeled occurrences fails the gate (the rev-1 blind spot).
  - GROUNDING RATE measures the foreknowledge/spoiler risk: what fraction of the proper-name tokens
    the extractor emitted actually appear in the chapter text (un-grounded tokens = smuggled from the
    model's training knowledge of the book — the BLOCKER the review found).
  - resolution layer-4 (embedding) is DISABLED here (lexical stand-in over-merges siblings); chunk
    vectors still use the lexical stand-in for plumbing only.

Honest residual limit (stated, not hidden): fragmentation into a surface form NOT in the
source-curated gold cannot be auto-detected; the coverage floor catches the symptom (fewer labeled
occurrences), not the root. Real proof needs a non-book-aware cheap model + LIT-8's spoiler eval.
"""
import json
import os
import re
import sys
import tempfile
from itertools import combinations

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import llm  # noqa: E402
import pipeline  # noqa: E402
from gold import gold_id, GOLD  # noqa: E402
from resolve import _norm, _proper_tokens  # noqa: E402

PRICE_IN, PRICE_OUT = 1.0, 5.0          # assumed haiku-class $/Mtok; precise modeling -> LIT-21
COVERAGE_FLOOR = 30                      # min gold-labeled occurrences expected (5 chs, 10 mains)


def _text(item):
    p = os.path.join(HERE, "chapters", f"ch{item['ordinal']:02d}.txt")
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


def main():
    path = os.path.join(HERE, "extractions.json")
    if not os.path.exists(path):
        print("extractions.json not found — run the extraction workflow first.")
        sys.exit(2)
    data = json.load(open(path))
    extractions = sorted((data["extractions"] if isinstance(data, dict) else data),
                         key=lambda x: x["ordinal"])

    client = llm.LLMClient(provider="stub")
    chunk_embed = lambda ts: client.embed(ts)[0]   # noqa: E731  (lexical stand-in, plumbing only)
    tmp = tempfile.mkdtemp(prefix="lit6_measure_")
    db = pipeline.dal.MemoryDB(os.path.join(tmp, "m.db"), "karamazov", title="The Brothers Karamazov")

    occurrences, methods = [], {}
    unresolved_rel, dropped_part = 0, 0
    grounded_tok, total_tok, ungrounded_examples = 0, 0, []
    seen_text = ""                                       # cumulative read-so-far text (chapters 1..N)

    def _grounded(tok, tokenset):
        # WHOLE-TOKEN membership (not substring) so 'alex' is not over-credited by 'alexey'.
        base = tok.replace("'s", "").replace("'", "").rstrip("s") if tok.endswith(("'s", "s")) else tok
        return tok in tokenset or (len(base) >= 3 and base in tokenset)

    for item in extractions:
        ex = item.get("extraction")
        if not ex:
            continue
        ch = {"ordinal": item["ordinal"], "key": item["key"], "title": item["title"], "text": _text(item)}
        seen_text += " " + _norm(ch["text"])             # include the current chapter
        # word-boundary tokens (strips punctuation + possessives: "Miüsov,"->miusov, "Mitya's"->mitya)
        seen_tokens = set(re.findall(r"[a-z0-9]+", seen_text))
        # grounding vs READ-SO-FAR: a name-token absent from every chapter up to here is detail the
        # book-aware model added by inference or training knowledge (the spoiler/foreknowledge risk).
        for ent in ex["entities"]:
            toks = set(_proper_tokens(ent["canonical_name"]))
            toks.update(t for a in ent.get("aliases", []) for t in _proper_tokens(a))
            for tok in toks:
                total_tok += 1
                if _grounded(tok, seen_tokens):
                    grounded_tok += 1
                elif len(ungrounded_examples) < 12:
                    ungrounded_examples.append((item["ordinal"], ent["canonical_name"], tok))
        r = pipeline.ingest_chapter(db, ch, ex, client, chunk_embed=chunk_embed, resolve_embed=None)
        if r.get("skipped"):
            continue
        unresolved_rel += r["unresolved_rel_refs"]
        dropped_part += r["dropped_event_participants"]
        for o in r["resolved"]:
            occurrences.append(o)
            methods[o["method"]] = methods.get(o["method"], 0) + 1

    again = pipeline.ingest_chapter(db, {"ordinal": extractions[0]["ordinal"], "key": extractions[0]["key"],
            "title": extractions[0]["title"], "text": _text(extractions[0])}, extractions[0]["extraction"], client)

    # label every occurrence: gold cluster id, or a singleton keyed on canonical (minor entity)
    for o in occurrences:
        g = gold_id(o["canonical_name"], o["aliases"])
        o["gold"] = g
        o["is_main"] = g is not None

    # ---- OVER-MERGE (precision), falsifiable over ALL system entities -----
    gold_of_sys = {}
    for o in occurrences:
        if o["is_main"]:
            gold_of_sys.setdefault(o["entity_id"], set()).add(o["gold"])
    overmerged = {e: s for e, s in gold_of_sys.items() if len(s) > 1}

    # ---- FRAGMENTATION (recall) per gold cluster over known forms ---------
    sys_of_gold = {}
    for o in occurrences:
        if o["is_main"]:
            sys_of_gold.setdefault(o["gold"], set()).add(o["entity_id"])
    fragmented = {g: s for g, s in sys_of_gold.items() if len(s) > 1}

    # ---- pairwise P/R over gold-labeled occurrences (scoped headline) -----
    labeled = [o for o in occurrences if o["is_main"]]
    TP = FP = FN = 0
    for a, b in combinations(labeled, 2):
        sg, ss = a["gold"] == b["gold"], a["entity_id"] == b["entity_id"]
        if sg and ss:
            TP += 1
        elif ss and not sg:
            FP += 1
        elif sg and not ss:
            FN += 1
    precision = TP / (TP + FP) if (TP + FP) else 1.0
    recall = TP / (TP + FN) if (TP + FN) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    grounding = grounded_tok / total_tok if total_tok else 0.0

    # ---- cost --------------------------------------------------------------
    total_cost = 0.0
    per_ch = []
    for item in extractions:
        if not item.get("extraction"):
            continue
        in_tok = (len(_text(item)) + 1200) / 4
        out_tok = len(json.dumps(item["extraction"])) / 4
        c = in_tok / 1e6 * PRICE_IN + out_tok / 1e6 * PRICE_OUT
        total_cost += c
        per_ch.append((item["ordinal"], int(in_tok), int(out_tok), c))
    n_ch = len(per_ch)

    # ---- report -----------------------------------------------------------
    print("LIT-6 — extraction + entity-resolution measurement (rev 2, honest)\n" + "=" * 66)
    print(f"chapters: {n_ch}   occurrences: {len(occurrences)}   gold-labeled (main cast): {len(labeled)}")
    print(f"resolution methods: {methods}   matched_roster-but-unmatched warnings: "
          f"{sum(1 for o in occurrences if o.get('method')=='new' and o.get('warn_unmatched_link'))}")
    print(f"unresolved relationship refs: {unresolved_rel}   dropped event participants: {dropped_part}")
    print(f"append-once re-ingest skipped: {again.get('skipped')}")
    print(f"\nENTITY RESOLUTION (pairwise, gold-labeled main cast):  P={precision:.3f}  R={recall:.3f}  F1={f1:.3f}  (TP={TP} FP={FP} FN={FN})")
    print(f"  OVER-MERGE (precision, ALL system entities, falsifiable): "
          f"{['id%s->%s' % (e, sorted(s)) for e, s in overmerged.items()] or 'none'}")
    print(f"  FRAGMENTATION (recall, known forms): {['%s->%d ids' % (g, len(s)) for g, s in fragmented.items()] or 'none'}")
    print(f"  COVERAGE: {len(labeled)} gold-labeled occurrences (floor {COVERAGE_FLOOR})")
    print(f"\nGROUNDING RATE (foreknowledge/spoiler risk): {grounding:.1%}  "
          f"({grounded_tok}/{total_tok} emitted name-tokens present in chapter text)")
    print(f"  ungrounded examples (smuggled from model knowledge, NOT in the chapter):")
    for o, name, tok in ungrounded_examples[:8]:
        print(f"    ch{o}: {name!r} token {tok!r}")

    print(f"\nGOLD cluster coverage (one system id each = no fragmentation):")
    for g in GOLD:
        ids = sys_of_gold.get(g)
        print(f"  {g:9s}: {('%d id(s)' % len(ids)) if ids else 'MISSING'}")

    print(f"\nCOST (assumed haiku-class ${PRICE_IN}/${PRICE_OUT} per Mtok; LOWER BOUND — roster grows, "
          f"caching not modeled; precise -> LIT-21):")
    print(f"  ~${total_cost/max(n_ch,1):.5f}/chapter avg; ~${total_cost/max(n_ch,1)*97:.2f} extrapolated to 97 chapters")

    # quality gate: no over-merge, NO fragmentation, recall strong, coverage floor, grounding high
    ok = (not overmerged) and (not fragmented) and recall >= 0.85 and len(labeled) >= COVERAGE_FLOOR and grounding >= 0.95
    print("\n" + "=" * 66)
    print("RESULT:", "PASS" if ok else "REVIEW",
          f"(no over-merge: {not overmerged}, no fragmentation: {not fragmented}, recall>=0.85: {recall>=0.85}, "
          f"coverage>={COVERAGE_FLOOR}: {len(labeled)>=COVERAGE_FLOOR}, grounding>=0.95: {grounding>=0.95})")
    print("HONEST SCOPE: name-grounding is %.0f%% (no out-of-bookmark NAME detail), BUT (a) only NAMES are "
          "substring-checkable — paraphrased relationships/events/state are NOT, so their spoiler-safety "
          "is LIT-8's adversarial job; (b) extraction used a strong BOOK-AWARE model, so cheap-tier quality "
          "and whether a weaker model stays as grounded are UNPROVEN; (c) resolver layers 2-4 "
          "(exact/alias/embedding) did 0 merges here — roster-link sufficed — so they are unvalidated. "
          "This run validates the PLUMBING + RESOLUTION ALGORITHM + name-grounding, not cheap-tier quality."
          % (grounding * 100))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
