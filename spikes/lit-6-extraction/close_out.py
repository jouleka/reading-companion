#!/usr/bin/env python3
"""LIT-6 empirical CLOSE-OUT — the part that needed a real backend (LIT-20 unblocked it).

Runs the PRODUCTION per-chapter loop with a REAL CHEAP model (extraction) + REAL embeddings
(resolution layer-4 + RAG), then measures the three numbers that were Provisional:
  - cheap-tier extraction quality  (resolution precision/recall on the gold main-cast clusters)
  - name-grounding                 (foreknowledge: name-tokens present in read-so-far prose)
  - real-embedding resolution      (layer-4 now ON with a real semantic embedder; method breakdown)
  + measured cost-per-chapter from REAL token usage.

Needs OPENAI_API_KEY (or any openai-compatible) + EMBED_PROVIDER=openai-compatible in the env.
Stdlib only. Run: env OPENAI_API_KEY=... EMBED_PROVIDER=openai-compatible python3 close_out.py
"""
import os
import re
import sys
import tempfile
from itertools import combinations

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "lit-5-schema"))
sys.path.insert(0, os.path.join(HERE, "..", "lit-20-llm-interface"))
import chapter_text  # noqa: E402
import llm  # noqa: E402
import pipeline  # noqa: E402
import versioning as V  # noqa: E402
from extract_schema import EXTRACTION_SCHEMA, EXTRACT_SYSTEM, extract_user_prompt, validate  # noqa: E402
from gold import gold_id, GOLD  # noqa: E402
from resolve import _norm, _proper_tokens  # noqa: E402

# gpt-4o-mini pricing (USD/Mtok); embeddings text-embedding-3-small ~ $0.02/Mtok (small, reported separately)
PRICE_IN, PRICE_OUT = 0.15, 0.60


def _grounded(tok, tokenset):
    base = tok.replace("'s", "").replace("'", "").rstrip("s") if tok.endswith(("'s", "s")) else tok
    return tok in tokenset or (len(base) >= 3 and base in tokenset)


def main():
    client = llm.LLMClient()
    print("LIT-6 close-out — REAL cheap model + REAL embeddings\n" + "=" * 64)
    print(f"completion: {client.provider}:{client.version['cheap']}   embed: {client.embed_identity()}")
    if client.provider == "stub" or client.embed_provider == "stub":
        print("ERROR: need a real completion key + EMBED_PROVIDER=openai-compatible in env.")
        sys.exit(2)
    EMBED = lambda ts: client.embed(ts)[0]  # noqa: E731
    ident = V.current_identity(client)

    tmp = tempfile.mkdtemp(prefix="lit6_closeout_")
    db = pipeline.dal.MemoryDB(os.path.join(tmp, "m.db"), "karamazov", title="The Brothers Karamazov")
    db.pin_models(ident["extractor_model"], ident["synth_model"], ident["embed_model"],
                  ident["embed_dim"], ident["embed_canary"])      # pin BEFORE any vector (LIT-20 rule)

    chs = chapter_text.chapter_texts(count=5)
    occurrences, methods = [], {}
    in_tok = out_tok = unresolved = invalid = 0
    seen_text, grounded_tok, total_tok = "", 0, 0
    for ch in chs:
        roster = pipeline.all_entities(db.view(max(ch["ordinal"] - 1, 0)))
        obj, u = client.complete(EXTRACT_SYSTEM, extract_user_prompt(ch["title"], roster, ch["text"]),
                                 tier="cheap", schema=EXTRACTION_SCHEMA)
        in_tok += u.get("in", 0)
        out_tok += u.get("out", 0)
        ok, _errs = validate(obj)
        invalid += 0 if ok else 1
        seen_text += " " + _norm(ch["text"])
        seen_tokens = set(re.findall(r"[a-zà-ÿ]+", seen_text))
        for e in obj.get("entities", []):
            toks = set(_proper_tokens(e["canonical_name"]))
            toks.update(t for a in e.get("aliases", []) for t in _proper_tokens(a))
            for tok in toks:
                total_tok += 1
                grounded_tok += 1 if _grounded(tok, seen_tokens) else 0
        r = pipeline.ingest_chapter(db, ch, obj, client, chunk_embed=EMBED, resolve_embed=EMBED)
        unresolved += r["unresolved_rel_refs"]
        for o in r["resolved"]:
            occurrences.append(o)
            methods[o["method"]] = methods.get(o["method"], 0) + 1
        print(f"  ch{ch['ordinal']} {ch['title'][:32]!r}: {len(obj.get('entities', []))} entities  "
              f"(in {u.get('in', 0)} / out {u.get('out', 0)} tok)")

    # ---- resolution precision/recall (pairwise, gold main cast) ----------
    labeled = [(o, gold_id(o["canonical_name"], o["aliases"])) for o in occurrences]
    labeled = [(o, g) for o, g in labeled if g]
    TP = FP = FN = 0
    for (oa, ga), (ob, gb) in combinations(labeled, 2):
        sg, ss = ga == gb, oa["entity_id"] == ob["entity_id"]
        TP += sg and ss
        FP += ss and not sg
        FN += sg and not ss
    P = TP / (TP + FP) if (TP + FP) else 1.0
    R = TP / (TP + FN) if (TP + FN) else 1.0
    overmerged = {}
    for o, g in labeled:
        overmerged.setdefault(o["entity_id"], set()).add(g)
    overmerged = {e: s for e, s in overmerged.items() if len(s) > 1}
    sys_of_gold = {}
    for o, g in labeled:
        sys_of_gold.setdefault(g, set()).add(o["entity_id"])
    fragmented = {g: s for g, s in sys_of_gold.items() if len(s) > 1}
    grounding = grounded_tok / total_tok if total_tok else 0.0
    cost = in_tok / 1e6 * PRICE_IN + out_tok / 1e6 * PRICE_OUT

    print("\n" + "=" * 64)
    print(f"REAL cheap-tier extraction quality (gpt-4o-mini):")
    print(f"  resolution  P={P:.3f}  R={R:.3f}  F1={2*P*R/(P+R) if P+R else 0:.3f}  (TP={TP} FP={FP} FN={FN})")
    print(f"  over-merge: {['id%s->%s' % (e, sorted(s)) for e, s in overmerged.items()] or 'none'}")
    print(f"  fragmentation: {['%s->%d ids' % (g, len(s)) for g, s in fragmented.items()] or 'none'}")
    print(f"  resolution methods (layer-4 embedding now REAL): {methods}")
    print(f"  unresolved rel refs: {unresolved}   invalid extractions: {invalid}")
    print(f"  GOLD coverage: " + ", ".join(f"{g}={'1' if g in sys_of_gold and len(sys_of_gold[g])==1 else ('FRAG' if g in sys_of_gold else 'MISS')}" for g in GOLD))
    print(f"  name-grounding (foreknowledge): {grounding:.1%}  ({grounded_tok}/{total_tok})")
    print(f"\nREAL cost: in={in_tok} out={out_tok} tok -> ${cost:.4f} for 5 chapters "
          f"(${cost/5:.5f}/ch); whole 97-ch book ~ ${cost/5*97:.3f}")
    gate = P >= 0.90 and R >= 0.80 and not overmerged
    print("\nRESULT:", "PASS" if gate else "REVIEW",
          f"(P>=0.90:{P>=0.90} R>=0.80:{R>=0.80} no over-merge:{not overmerged})")


if __name__ == "__main__":
    main()
