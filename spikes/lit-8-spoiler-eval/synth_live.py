#!/usr/bin/env python3
"""LIT-8 synthesis close-out — generate grounded-only recaps + an LLM-judge with a REAL model (via the
LIT-20 interface), writing synth_results.json for synth_measure.py. Replaces the agent-harness run with
the provider-agnostic client now that a key is available. Needs OPENAI_API_KEY (or any backend)."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "lit-6-extraction"))
import llm  # noqa: E402

JUDGE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["grounded", "unsupported_claims", "references_future", "notes"],
    "properties": {
        "grounded": {"type": "boolean"},
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
        "references_future": {"type": "boolean"},
        "notes": {"type": "string"},
    },
}
SYNTH_SYS = ("You write a spoiler-safe 'catch me up' recap. Use ONLY the supplied facts. Describe ONLY "
             "what has ALREADY happened. Do NOT add any character/event/outcome not in the facts, and do "
             "NOT foreshadow, 'set the stage', hint at, build anticipation for, or describe anything still "
             "to come — not even tension about a future meeting. No forward-looking sentences at all. "
             "Return ONLY the recap prose, past-tense.")
JUDGE_SYS = ("You adversarially audit a recap for OVER-REACH: list any claim NOT supported by the "
             "supplied facts (even plausible elaborations), and whether it references a later/future event.")


def main():
    client = llm.LLMClient()
    if client.provider == "stub":
        print("ERROR: need a real completion key in env.")
        sys.exit(2)
    facts = json.load(open(os.path.join(HERE, "synth_facts.json"), encoding="utf-8"))
    results = []
    for bm in ("1", "3", "5"):
        f = facts[bm]
        user = (f"Reader just finished chapter {bm}. Write a 4-6 sentence recap using ONLY these facts.\n\n"
                f"CHARACTERS: {', '.join(f['characters'])}\n\nCHAPTER SUMMARIES:\n- "
                + "\n- ".join(f["chapter_summaries"]) + "\n\nKEY EVENTS:\n- " + "\n- ".join(f["events"][:20]))
        recap, _ = client.complete(SYNTH_SYS, user, tier="large")
        jp = (f"ALLOWED FACTS (the only ones the writer could use):\nCHARACTERS: {', '.join(f['characters'])}\n"
              f"SUMMARIES: {' | '.join(f['chapter_summaries'])}\nEVENTS: {' | '.join(f['events'][:20])}\n\n"
              f"RECAP:\n\"\"\"{recap}\"\"\"\n\nList unsupported claims + whether it references a future event.")
        judge, _ = client.complete(JUDGE_SYS, jp, tier="large", schema=JUDGE_SCHEMA)
        results.append({"bookmark": int(bm), "recap": recap, "judge": judge})
        print(f"bm{bm}: recap {len(recap)} chars; judge grounded={judge['grounded']} "
              f"future={judge['references_future']} unsupported={len(judge['unsupported_claims'])}")
    json.dump({"results": results}, open(os.path.join(HERE, "synth_results.json"), "w", encoding="utf-8"),
              indent=1, ensure_ascii=False)
    print(f"wrote synth_results.json ({client.version['large']})")


if __name__ == "__main__":
    main()
