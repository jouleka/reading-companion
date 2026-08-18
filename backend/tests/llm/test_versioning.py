"""LIT-20 — model identity + version-pinning + safe-swap policy (productionized from
spikes/lit-20-llm-interface/versioning.py). These are pure functions over identity dicts whose
shape matches `book_meta`'s pinned columns, so they slot straight into the DAL's pin_models /
pinned_identity surface. Lifted near-verbatim (ADR 0007 D-A1 group (a)).
"""
import math

from app.llm import versioning as V
from app.llm.client import LLMClient


def _ident(**over):
    base = {
        "extractor_model": "openai-compatible:gpt-4o-mini",
        "synth_model": "openai-compatible:gpt-4o",
        "embed_model": "openai-compatible@https://api.openai.com/v1:text-embedding-3-small",
        "embed_dim": 3,
        "embed_canary": [1.0, 0.0, 0.0],
    }
    base.update(over)
    return base


def test_identical_identity_is_ok():
    assert V.safe_swap(_ident(), _ident()) == [("none", V.OK)]


def test_embed_model_change_forces_re_embed():
    out = V.safe_swap(_ident(), _ident(embed_model="other@x:m"))
    assert ("embed_model", V.FORCE_RE_EMBED) in out


def test_embed_dim_change_forces_re_embed():
    out = V.safe_swap(_ident(), _ident(embed_dim=4))
    assert ("embed_model", V.FORCE_RE_EMBED) in out


def test_canary_drift_forces_re_embed():
    # same model + dim, but the canary points the other way -> the space changed
    out = V.safe_swap(_ident(embed_canary=[1.0, 0.0, 0.0]),
                      _ident(embed_canary=[0.0, 1.0, 0.0]))
    assert ("embed_model", V.FORCE_RE_EMBED) in out


def test_canary_float_noise_does_not_false_trigger():
    # a real embedder's run-to-run jitter -> cosine ~0.99996, well above CANARY_COSINE_MIN
    base = [1.0, 0.0, 0.0]
    noisy = [1.0, 0.009, 0.0]                       # cosine ~0.99996
    assert math.isclose(V._cos(base, noisy), 1.0, abs_tol=1e-3)
    assert V.safe_swap(_ident(embed_canary=base), _ident(embed_canary=noisy)) == [("none", V.OK)]


def test_canary_just_below_threshold_forces_re_embed():
    # cos([1,0],[1,0.0548]) ~ 0.99850, just BELOW CANARY_COSINE_MIN (0.999) -> a real space change
    a, b = [1.0, 0.0], [1.0, 0.0548]
    assert V._cos(a, b) < V.CANARY_COSINE_MIN
    out = V.safe_swap(_ident(embed_dim=2, embed_canary=a), _ident(embed_dim=2, embed_canary=b))
    assert ("embed_model", V.FORCE_RE_EMBED) in out


def test_canary_just_above_threshold_is_ok():
    # cos([1,0],[1,0.0316]) ~ 0.99950, just ABOVE the threshold -> tolerated jitter, not a swap
    a, b = [1.0, 0.0], [1.0, 0.0316]
    assert V._cos(a, b) > V.CANARY_COSINE_MIN
    assert V.safe_swap(_ident(embed_dim=2, embed_canary=a), _ident(embed_dim=2, embed_canary=b)) == \
        [("none", V.OK)]


def test_null_pinned_canary_fails_closed():
    # name+dim match but the pinned book has NO canary (dal.pin_models(embed_canary=None)) -> cannot
    # certify same-space -> must FORCE_RE_EMBED, never silently OK (pass-1 HIGH).
    out = V.safe_swap(_ident(embed_canary=None), _ident(embed_canary=[1.0, 0.0, 0.0]))
    assert ("embed_model", V.FORCE_RE_EMBED) in out


def test_empty_current_canary_fails_closed():
    # the current client can't produce a canary (unreachable embedder at swap-check) -> fail closed
    out = V.safe_swap(_ident(embed_canary=[1.0, 0.0, 0.0]), _ident(embed_canary=[]))
    assert ("embed_model", V.FORCE_RE_EMBED) in out


def test_extractor_change_forces_re_extract():
    out = V.safe_swap(_ident(), _ident(extractor_model="openai-compatible:gpt-4o"))
    assert ("extractor_model", V.FORCE_RE_EXTRACT) in out


def test_synth_change_is_not_flagged():
    # synthesis is stateless (recap cache keys on it) -> a synth swap is spoiler-safe, never flagged
    assert V.safe_swap(_ident(), _ident(synth_model="openai-compatible:gpt-4o-2099")) == [("none", V.OK)]


def test_schema_version_change_migrates():
    out = V.safe_swap(_ident(schema_version=1), _ident(schema_version=2))
    assert ("schema_version", V.MIGRATE_SCHEMA) in out


def test_current_identity_matches_book_meta_shape():
    c = LLMClient(provider="stub", allow_stub=True)
    ci = V.current_identity(c)
    assert set(ci) >= {"extractor_model", "synth_model", "embed_model", "embed_dim", "embed_canary"}
    # embed_model is the FULL identity the embedder actually used, never a bare/unhonored name
    assert ci["embed_model"] == c.embed_identity() == "stub:lexical-stub-256"
    assert ci["embed_dim"] == len(c.embed(["x"])[0][0])
    assert isinstance(ci["embed_canary"], list) and ci["embed_canary"]


def test_current_identity_round_trips_through_safe_swap():
    c = LLMClient(provider="stub", allow_stub=True)
    ci = V.current_identity(c)
    # an identity compared to itself is always safe (canary cosine == 1.0)
    assert V.safe_swap(ci, V.current_identity(c)) == [("none", V.OK)]
