#!/usr/bin/env python3
"""LIT-6 / LIT-20 — the ONE provider-agnostic LLM + embedding interface.

Design directive (user): "any key, any provider, any subscription — like Hermes — or just
use ours." So this is a single tiny surface that any backend implements:

    client.complete(system, user, tier, schema=None) -> (obj_or_text, usage)
    client.embed(texts)                               -> (vectors, usage)
    client.version                                    -> stable stamp written onto every row

`tier` is "cheap" (per-chapter extraction) or "large" (lazy recap/notes synthesis) — the
two-tier split (D5). Backends auto-detected from the environment, in priority order:

  - anthropic            ANTHROPIC_API_KEY                 (cheap=Haiku, large=Sonnet/Opus)
  - openai-compatible    OPENAI_API_KEY / OPENAI_BASE_URL  (covers OpenAI, OpenRouter, Groq,
                         (or OPENROUTER_API_KEY, GROQ_…)    Together, LM Studio, llama.cpp,
                                                            Ollama's /v1 — i.e. ANY provider/sub)
  - stub                 (always available, offline)        deterministic; for CI + pipeline tests

No vendor SDK required — talks raw HTTP via `requests`. The model is NEVER hardcoded into the
pipeline; swapping providers is an env change. LIT-20 adds the version-pinning/safe-swap policy
on top of this surface (the `version` stamp here is the hook it builds on).
"""
import hashlib
import json
import math
import os

try:
    import requests
except Exception:                                   # pragma: no cover
    requests = None

# tier -> model, per provider. Overridable by env (LLM_CHEAP_MODEL / LLM_LARGE_MODEL).
_DEFAULT_MODELS = {
    "anthropic": {"cheap": "claude-haiku-4-5-20251001", "large": "claude-sonnet-4-6"},
    "openai-compatible": {"cheap": "gpt-4o-mini", "large": "gpt-4o"},
    "stub": {"cheap": "stub-cheap", "large": "stub-large"},
}


def _model_for(provider, tier):
    env = os.environ.get("LLM_CHEAP_MODEL" if tier == "cheap" else "LLM_LARGE_MODEL")
    return env or _DEFAULT_MODELS[provider][tier]


def detect_provider():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if any(os.environ.get(k) for k in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY",
                                       "TOGETHER_API_KEY", "OPENAI_BASE_URL")):
        return "openai-compatible"
    return "stub"


class LLMClient:
    def __init__(self, provider=None, embed_model=None):
        auto = provider is None
        self.provider = provider or detect_provider()
        if auto and self.provider == "stub":
            import sys
            print("WARNING: LLM provider auto-resolved to 'stub' (no API key detected) — the stub is "
                  "a crude offline heuristic, NOT a real extractor. Set ANTHROPIC_API_KEY or an "
                  "OpenAI-compatible key/base_url for real use.", file=sys.stderr)
        # openai-compatible config (base_url + key from whichever env is set)
        self.base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.api_key = (os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
                        or os.environ.get("GROQ_API_KEY") or os.environ.get("TOGETHER_API_KEY"))
        self.anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        # EMBEDDING backend — INDEPENDENT of the completion backend (Anthropic has no embeddings API;
        # a real Anthropic deployment pairs it with an openai-compatible embeddings endpoint). Resolved
        # from its OWN env so "Anthropic completion + OpenAI/Voyage/local embeddings" is expressible.
        self.embed_base_url = os.environ.get("EMBED_BASE_URL") or self.base_url
        self.embed_key = os.environ.get("EMBED_API_KEY") or os.environ.get("OPENAI_API_KEY")
        self.embed_provider = (os.environ.get("EMBED_PROVIDER")
                               or ("openai-compatible" if (os.environ.get("EMBED_BASE_URL") or os.environ.get("EMBED_API_KEY")) else "stub"))
        self.embed_model = (embed_model or os.environ.get("EMBED_MODEL") or os.environ.get("LLM_EMBED_MODEL")
                            or ("text-embedding-3-small" if self.embed_provider == "openai-compatible" else "lexical-stub-256"))
        self._warned_stub_embed = False

    @property
    def version(self):
        """Stable identity stamped onto every derived row (extractor_version) + every vector
        (embed). LIT-20 keys safe-swap / forced-re-embed off exactly this. `embed` is the identity
        of the embedder that embed() ACTUALLY uses (never a configured-but-unhonored name)."""
        return {"provider": self.provider,
                "cheap": _model_for(self.provider, "cheap"),
                "large": _model_for(self.provider, "large"),
                "embed": self.embed_identity()}

    def embed_identity(self):
        """The full, unambiguous identity of the embedder embed() will actually use — includes the
        provider + endpoint so a base_url repoint / stub-vs-real with the same model name do NOT
        collide. NEVER reports a configured name that wasn't honored."""
        if self.embed_provider == "openai-compatible" and self.embed_key:
            return f"openai-compatible@{self.embed_base_url}:{self.embed_model}"
        return "stub:lexical-stub-256"      # the actual embedder when no real endpoint is configured

    def extractor_version(self, tier="cheap"):
        return f"{self.provider}:{_model_for(self.provider, tier)}"

    # ---- completion -------------------------------------------------------
    def complete(self, system, user, tier="cheap", schema=None):
        if self.provider == "anthropic":
            return self._complete_anthropic(system, user, tier, schema)
        if self.provider == "openai-compatible":
            return self._complete_openai(system, user, tier, schema)
        return self._complete_stub(system, user, tier, schema)

    def _complete_anthropic(self, system, user, tier, schema):
        model = _model_for("anthropic", tier)
        body = {"model": model, "max_tokens": 4096, "system": system,
                "messages": [{"role": "user", "content": user}]}
        if schema:   # force a tool-call shaped to the schema (structured output)
            body["tools"] = [{"name": "emit", "description": "emit the extraction",
                              "input_schema": schema}]
            body["tool_choice"] = {"type": "tool", "name": "emit"}
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=120,
                          headers={"x-api-key": self.anthropic_key,
                                   "anthropic-version": "2023-06-01",
                                   "content-type": "application/json"}, json=body)
        r.raise_for_status()
        d = r.json()
        u = d["usage"]
        usage = {"in": u["input_tokens"], "out": u["output_tokens"],
                 "cache_write": u.get("cache_creation_input_tokens", 0),
                 "cache_read": u.get("cache_read_input_tokens", 0)}   # prompt-cache accounting (D5/LIT-21)
        if schema:
            obj = next(c["input"] for c in d["content"] if c["type"] == "tool_use")
            return obj, usage
        return "".join(c.get("text", "") for c in d["content"]), usage

    def _complete_openai(self, system, user, tier, schema):
        model = _model_for("openai-compatible", tier)
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        body = {"model": model, "messages": msgs}
        if schema:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "extraction", "schema": schema,
                                                       "strict": True}}
        r = requests.post(f"{self.base_url}/chat/completions", timeout=120,
                          headers={"Authorization": f"Bearer {self.api_key}",
                                   "content-type": "application/json"}, json=body)
        r.raise_for_status()
        d = r.json()
        u = d.get("usage", {})
        usage = {"in": u.get("prompt_tokens", 0), "out": u.get("completion_tokens", 0)}
        txt = d["choices"][0]["message"]["content"]
        return (json.loads(txt) if schema else txt), usage

    def _complete_stub(self, system, user, tier, schema):
        """Deterministic offline backend: enough structure to exercise the pipeline without a
        network. NOT a quality oracle — real validation uses a real backend (or the agent harness)."""
        if not schema:
            return "[stub recap] " + user[:120], {"in": len(user) // 4, "out": 30}
        # crude heuristic extraction: capitalized bigrams as 'characters'.
        import re
        names = []
        for m in re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b", user):
            if m not in names and m.lower() not in ("chapter", "the project"):
                names.append(m)
        ents = [{"canonical_name": n, "type": "character", "aliases": [],
                 "matched_roster": False, "state": None} for n in names[:8]]
        obj = {"chapter_summary": "[stub] " + user[:80], "entities": ents,
               "relationships": [], "events": [], "themes": []}
        return obj, {"in": len(user) // 4, "out": 50}

    # ---- embeddings (routed on the INDEPENDENT embedding backend) ----------
    def embed(self, texts):
        if self.embed_provider == "openai-compatible" and self.embed_key:
            r = requests.post(f"{self.embed_base_url}/embeddings", timeout=120,
                              headers={"Authorization": f"Bearer {self.embed_key}",
                                       "content-type": "application/json"},
                              json={"model": self.embed_model, "input": texts})
            r.raise_for_status()
            d = r.json()
            return [row["embedding"] for row in d["data"]], {"in": d.get("usage", {}).get("total_tokens", 0)}
        # No real embeddings endpoint -> deterministic LEXICAL stub so the cosine RESOLUTION ALGORITHM
        # is exercised. We WARN loudly (a configured-but-unreachable embedder is a silent-corruption
        # trap) and the identity we stamp is the ACTUAL embedder ("stub:lexical-stub-256"), never the
        # configured-but-unhonored name — embed_identity() guarantees this.
        if self.embed_provider != "stub" and not self._warned_stub_embed:
            import sys
            print(f"WARNING: embed provider '{self.embed_provider}' has no reachable endpoint/key — "
                  f"FALLING BACK to the lexical stub; these are NOT real embeddings and are stamped "
                  f"'stub:lexical-stub-256', not '{self.embed_model}'.", file=sys.stderr)
            self._warned_stub_embed = True
        return [_lexical_embed(t) for t in texts], {"in": 0}

    def embed_canary(self):
        """A fingerprint VECTOR of WHAT this embedder produces — embed a fixed probe and return the
        vector. Compared by COSINE with a tolerance (versioning.safe_swap), NOT exact equality, so it
        is robust to a real embedder's run-to-run numerical noise (~1e-4 from GPU/batch
        non-determinism) while still catching a genuine space change: two embedders sharing a model
        NAME (stub-vs-real, base_url repoint, silent re-train) produce a low-cosine canary -> a
        same-name swap is still FORCE_RE_EMBED. (Exact-hash was rejected: a vector sitting on a
        rounding boundary flips the hash on sub-1e-7 jitter.)"""
        return self.embed(["__lit20_embed_canary__"])[0][0]


def _lexical_embed(text, dim=256):
    v = [0.0] * dim
    t = "".join(ch.lower() if ch.isalnum() else " " for ch in text)
    toks = t.split()
    grams = toks + [t[i:i + 3] for i in range(len(t) - 2)]
    for g in grams:
        h = int(hashlib.md5(g.encode()).hexdigest(), 16)
        v[h % dim] += 1.0
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
