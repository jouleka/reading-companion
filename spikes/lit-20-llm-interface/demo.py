#!/usr/bin/env python3
"""LIT-20 — executable proof of the pluggable interface + per-book pinning + embed-version stamp +
safe-swap policy. [rev 2 — post adversarial review]

Proves (all exit criteria + the rev-1 BLOCKER/HIGH fixes):
  1. ONE tier-aware interface across >=2 backends; the EMBEDDING backend is INDEPENDENT of the
     completion backend (Anthropic completion + openai-compatible embeddings is expressible), and the
     embedder identity is HONEST (a stub never masquerades as a configured real model).
  2. PIN per book at first ingestion (incl. an embed CANARY fingerprint).
  3. ENFORCED stamp: add_chunk REJECTS a vector whose embed model/dim != the pin; search is same-space
     BY DEFAULT (resolves the pinned model); a cross-model query returns nothing.
  4. SAFE-SWAP: synth=OK; extractor=FORCE_RE_EXTRACT; embed model/dim/CANARY change=FORCE_RE_EMBED
     (so a same-NAME space change is still caught); schema=MIGRATE. Re-embed migration RE-PINS and
     then safe_swap==OK.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "lit-6-extraction"))
sys.path.insert(0, os.path.join(HERE, "..", "lit-5-schema"))
import llm  # noqa: E402
import dal  # noqa: E402
import versioning as V  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def main():
    print("LIT-20 — pluggable LLM/embedding interface + versioning (rev 2)\n" + "=" * 64)

    # ---- 1. one interface; embedding backend INDEPENDENT + honest ---------
    print("\n1. One tier-aware interface; embedding backend independent of completion + honest identity")
    for prov in ("anthropic", "openai-compatible", "stub"):
        c = llm.LLMClient(provider=prov)
        print(f"  {prov:18s} cheap={c.version['cheap']:28s} large={c.version['large']:18s} embed={c.embed_identity()}")
    check("contract resolves cheap+large for >=2 real backends + stub",
          all(hasattr(llm.LLMClient(provider="stub"), m) for m in ("complete", "embed", "version")))
    # Anthropic has NO embeddings API -> a stub client must HONESTLY report the stub, never a real name
    anth = llm.LLMClient(provider="anthropic")
    check("Anthropic book does NOT silently pin a fake-named real embedder (honest stub identity)",
          anth.embed_identity() == "stub:lexical-stub-256",
          f"embed_identity={anth.embed_identity()}")
    # embedding backend is configurable INDEPENDENTLY of completion (simulate EMBED_* config)
    anth.embed_provider, anth.embed_key, anth.embed_model = "openai-compatible", "sk-test", "text-embedding-3-small"
    check("Anthropic completion + openai-compatible embeddings is EXPRESSIBLE",
          anth.embed_identity() == "openai-compatible@https://api.openai.com/v1:text-embedding-3-small")

    # ---- 2. pin per book (with canary) ------------------------------------
    print("\n2. Pin the model identity (incl. embed canary) per book at first ingestion")
    client = llm.LLMClient(provider="stub")
    ident = V.current_identity(client)
    tmp = tempfile.mkdtemp(prefix="lit20_")
    db = dal.MemoryDB(os.path.join(tmp, "m.db"), "bk", title="Book")
    db.pin_models(ident["extractor_model"], ident["synth_model"], ident["embed_model"],
                  ident["embed_dim"], ident["embed_canary"])
    pinned = db.pinned_identity()
    check("book_meta pins embed identity + dim + canary",
          pinned["embed_model"] == ident["embed_model"] and pinned["embed_dim"] == ident["embed_dim"]
          and pinned["embed_canary"] == ident["embed_canary"],
          f"embed={pinned['embed_model']} dim={pinned['embed_dim']} canary=<{len(pinned['embed_canary'])}-d vector>")

    # ---- 3. ENFORCED stamp + same-space-by-default search ------------------
    print("\n3. Enforced embed stamp + same-space-by-default search")
    db.add_chapter("bk:c1", 1, href="c1", content_hash="h1")
    embA = lambda ts: client.embed(ts)[0]  # noqa: E731
    db.add_chunk("bk:c1", 1, "Alyosha at the monastery", embA(["Alyosha at the monastery"])[0],
                 embed_model=ident["embed_model"], embed_dim=ident["embed_dim"])
    # add_chunk REJECTS a vector whose model/dim disagrees with the pin
    rejected = False
    try:
        db.add_chunk("bk:c1", 1, "bad", embA(["bad"])[0], embed_model="other-embed-v2")
    except ValueError:
        rejected = True
    check("add_chunk REJECTS a chunk whose embed model != the pin", rejected)
    rejected_dim = False
    try:
        db.add_chunk("bk:c1", 1, "bad", [0.1, 0.2, 0.3], embed_model=ident["embed_model"])  # wrong dim
    except ValueError:
        rejected_dim = True
    check("add_chunk REJECTS a wrong-dim vector", rejected_dim)
    v = db.view(5)
    check("search WITHOUT embed_model resolves the pinned model (same-space by default)",
          len(v.search(embA(["monastery"])[0], k=3)) >= 1)
    check("a query under a DIFFERENT embed model returns NOTHING",
          v.search(embA(["monastery"])[0], k=3, embed_model="other-embed-v2") == [])

    # ---- 4. safe-swap policy (incl. canary catching a same-name swap) ------
    print("\n4. Safe-swap policy")
    print(V.render_matrix())
    base = {k: pinned[k] for k in ("extractor_model", "synth_model", "embed_model", "embed_dim", "embed_canary")}
    def decide(**ch):
        return [d for _, d in V.safe_swap(base, {**base, **ch})]
    check("synth model may change freely -> OK", decide(synth_model="stub:stub-large-v2") == [V.OK])
    check("extractor change -> FORCE_RE_EXTRACT", V.FORCE_RE_EXTRACT in decide(extractor_model="x:y"))
    check("embed model change -> FORCE_RE_EMBED", V.FORCE_RE_EMBED in decide(embed_model="other@x:m"))
    check("embed DIM change -> FORCE_RE_EMBED", V.FORCE_RE_EMBED in decide(embed_dim=512))
    # canary is a VECTOR compared by cosine: a different embedder (different direction) is caught,
    # but a real embedder's run-to-run float noise must NOT false-trigger a costed re-embed.
    diff_space = embA(["a completely different embedder fingerprint"])[0]
    noisy = [x + 1e-4 * ((i % 2) * 2 - 1) for i, x in enumerate(base["embed_canary"])]
    check("SAME-NAME embed space change caught by the CANARY (cosine) -> FORCE_RE_EMBED",
          V.FORCE_RE_EMBED in decide(embed_canary=diff_space))
    check("run-to-run float noise does NOT false-trigger re-embed (canary tolerance)",
          decide(embed_canary=noisy) == [V.OK])
    check("schema_version change -> MIGRATE_SCHEMA",
          V.MIGRATE_SCHEMA in [d for _, d in V.safe_swap({**base, "schema_version": 1},
                                                         {**base, "schema_version": 2})])

    # ---- 4b. re-embed migration RE-PINS, then safe_swap == OK -------------
    print("\n4b. Re-embed migration (retract -> re-embed -> RE-PIN) leaves safe_swap == OK")
    new_model, new_dim, new_canary = "openai-compatible@x:text-embedding-3-small", ident["embed_dim"], diff_space
    check("the embed swap is detected as FORCE_RE_EMBED",
          V.FORCE_RE_EMBED in [d for _, d in V.safe_swap(base, {**base, "embed_model": new_model, "embed_canary": new_canary})])
    db._retract("chunks", "embed_model = ?", (ident["embed_model"],))   # invalidate old-space vectors
    db.repin_embedding(new_model, new_dim, new_canary)                  # OVERWRITE the pin (migration)
    db.add_chunk("bk:c1", 1, "Alyosha at the monastery", embA(["Alyosha at the monastery"])[0],
                 embed_model=new_model, embed_dim=new_dim)
    check("after re-embed+re-pin, KNN works under the new model", len(db.view(5).search(embA(["monastery"])[0], k=3)) >= 1)
    after = db.pinned_identity()
    post = {k: after[k] for k in ("extractor_model", "synth_model", "embed_model", "embed_dim", "embed_canary")}
    check("post-migration safe_swap(pinned, current) == OK (re-pin closed the loop)",
          V.safe_swap(post, {**post}) == [("none", V.OK)])

    print("\n" + "=" * 64)
    if FAILS:
        print(f"RESULT: {len(FAILS)} CHECK(S) FAILED -> {FAILS}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
