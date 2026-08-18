#!/usr/bin/env python3
"""LIT-26 — the D-A4 vec0 gate spike: may `sqlite-vec` vec0 ANN replace BruteForce under the spoiler
funnel? Three questions, all answered empirically here:

  (a) RECALL: does the `revealed_at` range pre-filter inside a vec0 KNN query return the SAME top-k
      as BruteForce cosine over the funnel-filtered candidates? Measured on the REAL 96 live
      Karamazov vectors (text-embedding-3-small, unit-norm -> L2 order == cosine order) and on a
      10k-vector synthetic scale-up.
  (b) FUNNEL EQUIVALENCE + FALSIFIABILITY: the vec0 query must express `revealed_at <= bm AND
      retracted_at IS NULL`-equivalent bounds; dropping the bound must demonstrably surface a future
      chunk (the leak the funnel exists to stop).
  (c) SHADOW TABLES + AUTHORIZER: what base tables does vec0 create, what does the SQLite authorizer
      see when reading through the virtual table, and what would the DAL's fail-closed
      `INFRA_TABLES` allow-list need?

Read-only against the live store (URI mode=ro). Stdlib + sqlite_vec only. Exit 0 = data gathered;
the GO/NO-GO verdict prints at the end.
"""
import json
import math
import os
import random
import sqlite3
import sys
import time

import sqlite_vec

LIVE = os.environ.get("READING_COMPANION_PROBE_DB")


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def brute_topk(rows, qv, bm, k):
    cand = [(cosine(qv, v), cid) for (cid, rev, v) in rows if rev <= bm]
    cand.sort(key=lambda t: (-t[0], t[1]))
    return [cid for _s, cid in cand[:k]]


def vec_conn():
    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    return db


def serialize(v):
    return sqlite_vec.serialize_float32(v)


TIE_EPS = 1e-4          # a swapped pair within this cosine gap is a float32 ordering tie, not a loss


def recall_table(rows, dim, label, bms, ks, n_queries=8, seed=7):
    """Build a vec0 table with a revealed_at metadata column; compare prefiltered KNN vs brute.
    A mismatch counts as REAL recall loss only when the swapped items' cosine gap exceeds TIE_EPS —
    vec0 stores float32 and accumulates L2, so a ~1e-5 boundary tie can legitimately flip order."""
    db = vec_conn()
    db.execute(f"CREATE VIRTUAL TABLE v USING vec0(embedding float[{dim}], revealed_at integer)")
    with db:
        for (cid, rev, vec) in rows:
            db.execute("INSERT INTO v(rowid, embedding, revealed_at) VALUES (?,?,?)",
                       (cid, serialize(vec), rev))
    by_id = {cid: v for (cid, _rev, v) in rows}
    rng = random.Random(seed)
    total = agree = ties = real_losses = 0
    for bm in bms:
        for k in ks:
            for _ in range(n_queries):
                base = rng.choice(rows)[2]
                qv = [x + rng.gauss(0, 0.02) for x in base]      # a near-duplicate query
                want = brute_topk(rows, qv, bm, k)
                got = [r[0] for r in db.execute(
                    "SELECT rowid FROM v WHERE embedding MATCH ? AND k = ? AND revealed_at <= ? "
                    "ORDER BY distance", (serialize(qv), k, bm)).fetchall()]
                if not want:
                    continue
                total += 1
                if set(want) == set(got):
                    agree += 1
                    continue
                gaps = [abs(cosine(qv, by_id[m]) - cosine(qv, by_id[e]))
                        for m in set(want) - set(got) for e in set(got) - set(want)]
                if gaps and max(gaps) < TIE_EPS:
                    ties += 1
                else:
                    real_losses += 1
                    print(f"    REAL LOSS bm={bm} k={k}: missing={set(want)-set(got)} gapmax={max(gaps):.2e}")
    print(f"  [{label}] {total} queries: exact {agree}, float32-ties {ties}, REAL losses {real_losses}")
    return db, real_losses


print("LIT-26 vec0 gate spike")
print("=" * 64)
print(f"sqlite-vec version: {vec_conn().execute('SELECT vec_version()').fetchone()[0]}")

if not LIVE:
    raise SystemExit("set READING_COMPANION_PROBE_DB to a local memory.db path")

# ---- (a) REAL vectors --------------------------------------------------------
src = sqlite3.connect(f"file:{LIVE}?mode=ro", uri=True)
src.row_factory = sqlite3.Row
real = [(r["chunk_id"], r["revealed_at"], json.loads(r["vec"]))
        for r in src.execute("SELECT chunk_id, revealed_at, vec FROM chunks "
                             "WHERE retracted_at IS NULL AND embed_model IS NOT NULL")]
src.close()
dim = len(real[0][2])
print(f"\n(a) REAL live vectors: {len(real)} chunks, dim {dim} (unit-norm; L2 order == cosine order)")
db_real, losses_real = recall_table(real, dim, "real-96", bms=(2, 10, 48, 96), ks=(3, 5, 10))

# ---- (a2) synthetic scale-up -------------------------------------------------
rng = random.Random(42)
def unit(dimn):
    v = [rng.gauss(0, 1) for _ in range(dimn)]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]
synth = [(i + 1, rng.randint(1, 96), unit(256)) for i in range(10_000)]
t0 = time.time()
db_syn, losses_syn = recall_table(synth, 256, "synthetic-10k", bms=(5, 30, 60, 96), ks=(5, 10),
                                  n_queries=5)
print(f"  (synthetic run {time.time()-t0:.0f}s)")

# ---- (b) falsifiability: drop the bound -> the future chunk surfaces ----------
future_cid = max(real, key=lambda r: r[1])[0]
qv = next(v for cid, _rev, v in real if cid == future_cid)
bounded = [r[0] for r in db_real.execute(
    "SELECT rowid FROM v WHERE embedding MATCH ? AND k = 3 AND revealed_at <= 2 ORDER BY distance",
    (serialize(qv), )).fetchall()]
unbounded = [r[0] for r in db_real.execute(
    "SELECT rowid FROM v WHERE embedding MATCH ? AND k = 3 ORDER BY distance",
    (serialize(qv), )).fetchall()]
print(f"\n(b) falsifiability: querying WITH the last chapter's own vector at bm=2:")
print(f"    bounded  top-3 (rev<=2): {bounded}  -> future chunk present: {future_cid in bounded}")
print(f"    UNbounded top-3:         {unbounded} -> future chunk present: {future_cid in unbounded}")
leak_demonstrable = (future_cid not in bounded) and (future_cid in unbounded)

# ---- (c) shadow tables + authorizer -------------------------------------------
db3 = vec_conn()
db3.execute("CREATE VIRTUAL TABLE chunks_vec USING vec0(embedding float[4], revealed_at integer)")
shadow = [r[0] for r in db3.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name != 'chunks_vec'").fetchall()]
print(f"\n(c) shadow tables created by vec0 'chunks_vec': {shadow}")
seen = set()
def auth(action, a1, a2, dbn, trig):
    if action == sqlite3.SQLITE_READ and a1:
        seen.add(a1)
    return sqlite3.SQLITE_OK
with db3:
    db3.execute("INSERT INTO chunks_vec(rowid, embedding, revealed_at) VALUES (1, ?, 1)",
                (serialize([1.0, 0, 0, 0]),))
db3.set_authorizer(auth)
db3.execute("SELECT rowid FROM chunks_vec WHERE embedding MATCH ? AND k = 1",
            (serialize([1.0, 0, 0, 0]),)).fetchall()
db3.set_authorizer(None)
print(f"    tables the AUTHORIZER saw during a vec0 KNN read: {sorted(seen)}")
print("    -> the DAL INFRA_TABLES allow-list needs the 'chunks_vec_*' shadow family; the virtual")
print("       table itself must be authorizer-guarded like a fact table (it holds the vectors).")

# ---- verdict -------------------------------------------------------------------
go = losses_real == 0 and losses_syn == 0 and leak_demonstrable
print("\n" + "=" * 64)
print(f"VERDICT: {'GO (conditions apply — see the LIT-26 ticket)' if go else 'NO-GO'} — "
      f"{'zero filter-induced recall loss (float32 boundary ties only) and the leak is demonstrable'
         if go else 'a REAL recall loss or a falsifiability failure occurred'}")
sys.exit(0)
