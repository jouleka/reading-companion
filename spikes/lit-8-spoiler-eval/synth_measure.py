#!/usr/bin/env python3
"""LIT-8 — synthesis over-reach scoring (vector 3, the LLM part).

Combines the DETERMINISTIC future-entity check (score_recap) with the LLM-JUDGE verdicts on REAL
grounded-only recaps generated at several bookmarks (from the lit8-synthesis-eval workflow ->
synth_results.json). Reports:
  - hard future-entity leak rate (a recap naming an entity revealed only later) — must be 0;
  - LLM-judged paraphrase over-reach (claims unsupported by the supplied facts);
  - references_future flags.
Run AFTER synth_results.json exists.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness  # noqa: E402

FAILS = []


def main():
    path = os.path.join(HERE, "synth_results.json")
    if not os.path.exists(path):
        print("synth_results.json not found — run the lit8-synthesis-eval workflow first.")
        sys.exit(2)
    data = json.load(open(path, encoding="utf-8"))
    results = data["results"] if isinstance(data, dict) else data
    db, max_bm, texts, client = harness.build_store()

    print("LIT-8 — synthesis over-reach (deterministic future-entity + prolepsis + LLM-judge)\n" + "=" * 70)
    hard_leaks = prolepsis_hits = overreach = future_refs = missing_judge = 0
    for item in sorted(results, key=lambda x: x["bookmark"]):
        bm, recap, judge = item["bookmark"], item["recap"], item.get("judge")
        det = harness.score_recap(db, bm, recap, read_text=harness.read_text_upto(texts, bm))
        leak, prol = det["future_entity_leaks"], det["prolepsis_hits"]
        if judge is None:                               # FAIL-CLOSED: an unjudged recap is not "clean"
            missing_judge += 1
            judge = {"grounded": None, "references_future": True, "unsupported_claims": []}
        unsup = judge.get("unsupported_claims", [])
        fut = judge.get("references_future", False)
        hard_leaks += len(leak)
        prolepsis_hits += len(prol)
        overreach += len(unsup)
        future_refs += 1 if fut else 0
        print(f"\nbookmark {bm}:")
        print(f"  recap: {recap[:150].strip()}...")
        print(f"  [deterministic] future-entity leaks: {leak or 'none'}   prolepsis: {prol or 'none'}   "
              f"grounded_rate={det['grounded_rate']:.2f}")
        print(f"  [LLM-judge] grounded={judge.get('grounded')}  references_future={fut}  "
              f"unsupported_claims={len(unsup)}")
        for c in unsup[:3]:
            print(f"      - {c}")

    print("\n" + "=" * 70)
    n = len(results)
    print(f"SUMMARY over {n} recaps:  hard future-entity leaks={hard_leaks}  prolepsis hits={prolepsis_hits}  "
          f"future-references(judge)={future_refs}  missing-judge={missing_judge}  soft over-reach claims={overreach}")
    # HARD gate (fail-closed): no future-entity name, no prolepsis/future-tense, no judge-flagged future
    # reference, no missing judge. Soft paraphrase over-reach is reported, not gated.
    ok = hard_leaks == 0 and prolepsis_hits == 0 and future_refs == 0 and missing_judge == 0
    print("RESULT:", "PASS" if ok else "REVIEW",
          f"(future-entity={hard_leaks == 0}, prolepsis={prolepsis_hits == 0}, "
          f"judge-future-ref={future_refs == 0}, judged={missing_judge == 0})")
    print("Guardrail: grounded-only prompt + DETERMINISTIC future-entity(case-insensitive)+prolepsis check "
          "(hard) + LLM-judge (hard on references_future, soft on paraphrase). RESIDUAL: a future event in "
          "PAST tense with no name rests on the soft judge — a deterministic NLI/span event-grounding gate "
          "is routed to the build (ADR 0004). Synthesis used a book-aware model; cheap-tier -> LIT-20.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
