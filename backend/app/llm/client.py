"""LIT-20 / LIT-6 — the ONE provider-agnostic LLM + embedding client.

Re-ported (ADR 0007 D-A1 group (b), D-A5) from ``spikes/lit-6-extraction/llm.py`` with named,
behaviour-relevant changes:

  * **Pydantic models are the schema source of truth.** ``complete(..., schema=<PydanticModel>)``
    returns a validated instance handed downstream **as a dict via ``model_dump(mode="json")``**, so
    the pipeline/gate's dict-of-strings contract is unchanged (``str``-subclass enums serialize to
    their string values).
  * **OpenAI structured output uses the SDK's native strict helper** (``chat.completions.parse``):
    the strict-mode transform (recursive ``additionalProperties:false`` + every property ``required``)
    is applied by the SDK, which raw ``model_json_schema()`` does NOT satisfy. Strict ``json_schema``
    is OpenAI-specific; other OpenAI-compatible providers get the same strict transform via
    ``to_strict_json_schema`` **plus a Pydantic-validate backstop + one re-ask** (a looser/malformed
    output → re-ask → fail-closed, which fails toward under-extraction, the safe direction).
  * **Embeddings are configured INDEPENDENTLY** of completion (``EMBED_*`` env / kwargs), routed
    around the structured path. ``embed()`` never lies about which embedder ran (``embed_identity``;
    the stub stamps ``stub:lexical-stub-256``, surfaced as a real warning, never a configured-but-
    unhonored name).
  * The **offline stub** is retained for CI/determinism. A stub *completion* provider is silent
    garbage, so it is **default-deny**: constructing one without ``allow_stub=True`` raises
    (ADR 0007 D-A7 / P2-14).

Backends auto-detected from the environment, in priority order: ``anthropic`` (ANTHROPIC_API_KEY),
``openai-compatible`` (OPENAI/OPENROUTER/GROQ/TOGETHER key or OPENAI_BASE_URL — covers OpenAI plus any
OpenAI-compatible endpoint), else ``stub``. ``tier`` is ``"cheap"`` (per-chapter extraction) or
``"large"`` (lazy recap/notes synthesis) — the two-tier split (D5).
"""
import hashlib
import json
import math
import os
import sys
import threading
import time

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, BadRequestError, OpenAI
from openai.lib._pydantic import to_strict_json_schema
from pydantic import ValidationError

# tier -> model, per provider. Overridable by env (LLM_CHEAP_MODEL / LLM_LARGE_MODEL) or kwargs.
_DEFAULT_MODELS = {
    "anthropic": {"cheap": "claude-haiku-4-5-20251001", "large": "claude-sonnet-4-6"},
    "openai-compatible": {"cheap": "gpt-4o-mini", "large": "gpt-4o"},
    "stub": {"cheap": "stub-cheap", "large": "stub-large"},
}
_OPENAI_DEFAULT_BASE = "https://api.openai.com/v1"
_ANTHROPIC_RETRY_STATUS = (429, 500, 502, 503, 504)


class ProviderUnavailable(RuntimeError):
    """Secret-free provider failure safe to route through operational and reader-facing surfaces."""

    def __init__(self, kind: str, *, service: str):
        self.kind = kind
        self.service = service
        super().__init__(f"AI provider {service} is unavailable ({kind})")

    def public_detail(self) -> dict[str, str]:
        if self.kind == "authentication":
            return {
                "code": "provider_authentication_failed",
                "message": "The AI provider rejected the configured credentials.",
            }
        if self.kind == "rate_limit":
            return {
                "code": "provider_rate_limited",
                "message": "The AI provider is temporarily rate limited.",
            }
        return {
            "code": "provider_unavailable",
            "message": "The AI provider is temporarily unavailable.",
        }


def _provider_failure_kind(exc: Exception) -> str:
    status = None
    if isinstance(exc, APIStatusError):
        status = exc.status_code
    elif isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    if status in (401, 403):
        return "authentication"
    if status == 429:
        return "rate_limit"
    if isinstance(exc, (APIConnectionError, APITimeoutError, httpx.TransportError)):
        return "network"
    return "service"


def detect_provider(env):
    if env.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if any(env.get(k) for k in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY",
                                "TOGETHER_API_KEY", "OPENAI_BASE_URL")):
        return "openai-compatible"
    return "stub"


class LLMClient:
    def __init__(self, *, provider=None, api_key=None, base_url=None, anthropic_key=None,
                 embed_provider=None, embed_base_url=None, embed_key=None, embed_model=None,
                 cheap_model=None, large_model=None, allow_stub=False,
                 http_client=None, embed_http_client=None, env=None,
                 max_retries=2, timeout=120.0, sleep=None):
        env = os.environ if env is None else env
        self.env = env
        self.max_retries = max_retries
        self.timeout = timeout
        self._sleep = sleep or time.sleep
        self.cheap_model = cheap_model
        self.large_model = large_model
        self._health_lock = threading.Lock()
        initial_status = "ready" if (provider == "stub" or (provider is None and detect_provider(env) == "stub")) else "unchecked"
        self._provider_health = {
            "completion": {"status": initial_status, "reason": None},
            "embedding": {"status": initial_status, "reason": None},
        }

        self.provider = provider or detect_provider(env)
        if self.provider == "stub" and not allow_stub:
            raise RuntimeError(
                "LLM provider resolved to the offline 'stub' (no API key detected and none supplied) "
                "— the stub is a crude offline heuristic, NOT a real extractor. Set ANTHROPIC_API_KEY "
                "or an OpenAI-compatible key/base_url, or pass allow_stub=True (CI/tests only).")
        if self.provider == "stub":
            print("WARNING: LLM provider is the offline 'stub' (allow_stub=True) — crude heuristic "
                  "output only, not a real extractor.", file=sys.stderr)

        # completion (openai-compatible) config
        self.base_url = base_url or env.get("OPENAI_BASE_URL") or _OPENAI_DEFAULT_BASE
        self.api_key = (api_key or env.get("OPENAI_API_KEY") or env.get("OPENROUTER_API_KEY")
                        or env.get("GROQ_API_KEY") or env.get("TOGETHER_API_KEY"))
        self.anthropic_key = anthropic_key or env.get("ANTHROPIC_API_KEY")
        self._is_native_openai = self.base_url.rstrip("/") == _OPENAI_DEFAULT_BASE
        # A non-OpenAI compatible key (OpenRouter/Groq/Together) aimed at the DEFAULT api.openai.com would
        # take the OpenAI-only strict .parse path with the wrong key and auth-fail confusingly. Fail loud
        # at construction with an actionable message instead (pass-1 review LOW).
        if (self.provider == "openai-compatible" and self._is_native_openai and not base_url
                and not env.get("OPENAI_BASE_URL") and not (api_key or env.get("OPENAI_API_KEY"))
                and any(env.get(k) for k in ("OPENROUTER_API_KEY", "GROQ_API_KEY", "TOGETHER_API_KEY"))):
            raise RuntimeError(
                "a non-OpenAI compatible key (OpenRouter/Groq/Together) was detected but no base_url is "
                "set — set OPENAI_BASE_URL (or pass base_url=) to that provider's OpenAI-compatible "
                "endpoint; the default api.openai.com would reject the key.")

        # EMBEDDING backend — INDEPENDENT of the completion backend (Anthropic has no embeddings API;
        # a real Anthropic deployment pairs it with an OpenAI-compatible embeddings endpoint).
        self.embed_base_url = embed_base_url or env.get("EMBED_BASE_URL") or self.base_url
        same_endpoint = self.embed_base_url == self.base_url
        # The completion key is reused for embeddings ONLY when they target the SAME endpoint — never
        # sent to a different EMBED_BASE_URL host (that would leak the key to a foreign embedder, against
        # D19's independence rule; pass-2 review). An explicit EMBED_API_KEY always wins and may target a
        # foreign endpoint.
        self.embed_key = (embed_key or env.get("EMBED_API_KEY")
                          or ((env.get("OPENAI_API_KEY") or self.api_key) if same_endpoint else None))
        # Resolve the embed provider: explicit > EMBED_* env > "a real SAME-ENDPOINT completion key
        # implies a real embedder" (so a lone OPENAI_API_KEY / programmatic api_key doesn't SILENTLY
        # embed via the lexical stub while completion runs on a real model; pass-2 review) > stub.
        self.embed_provider = (
            embed_provider or env.get("EMBED_PROVIDER")
            or ("openai-compatible" if (env.get("EMBED_BASE_URL") or env.get("EMBED_API_KEY")
                or (self.provider == "openai-compatible" and self.embed_key and same_endpoint))
                else "stub"))
        self.embed_model = (embed_model or env.get("EMBED_MODEL") or env.get("LLM_EMBED_MODEL")
                            or ("text-embedding-3-small" if self.embed_provider == "openai-compatible"
                                else "lexical-stub-256"))
        self._warned_stub_embed = False

        # SDK / transport handles (lazy where a provider isn't used)
        self._oai = None
        self._embed_oai = None
        self._http = None
        if self.provider == "openai-compatible":
            self._oai = OpenAI(api_key=self.api_key or "missing", base_url=self.base_url,
                               max_retries=max_retries, timeout=timeout, http_client=http_client)
        elif self.provider == "anthropic":
            self._http = http_client or httpx.Client(timeout=timeout)
        if self.embed_provider == "openai-compatible" and self.embed_key:
            self._embed_oai = OpenAI(api_key=self.embed_key, base_url=self.embed_base_url,
                                     max_retries=max_retries, timeout=timeout,
                                     http_client=embed_http_client)

    # ---- model identity ----------------------------------------------------
    def _model_for(self, tier):
        if tier == "cheap":
            return self.cheap_model or self.env.get("LLM_CHEAP_MODEL") or _DEFAULT_MODELS[self.provider]["cheap"]
        return self.large_model or self.env.get("LLM_LARGE_MODEL") or _DEFAULT_MODELS[self.provider]["large"]

    @property
    def version(self):
        """Stable identity stamped onto every derived row (extractor_version) + every vector (embed).
        ``embed`` is the identity of the embedder ``embed()`` ACTUALLY uses (never a configured-but-
        unhonored name)."""
        return {"provider": self.provider, "cheap": self._model_for("cheap"),
                "large": self._model_for("large"), "embed": self.embed_identity()}

    def embed_identity(self):
        """The full, unambiguous identity of the embedder ``embed()`` will actually use — includes the
        provider + endpoint so a base_url repoint / stub-vs-real with the same model name do NOT
        collide. NEVER reports a configured name that wasn't honored."""
        if self.embed_provider == "openai-compatible" and self.embed_key:
            return f"openai-compatible@{self.embed_base_url}:{self.embed_model}"
        return "stub:lexical-stub-256"

    def extractor_version(self, tier="cheap"):
        return f"{self.provider}:{self._model_for(tier)}"

    # ---- completion --------------------------------------------------------
    def complete(self, system, user, tier="cheap", schema=None, max_output_tokens=None):
        try:
            if self.provider == "anthropic":
                result = self._complete_anthropic(system, user, tier, schema, max_output_tokens)
            elif self.provider == "openai-compatible":
                result = self._complete_openai(system, user, tier, schema, max_output_tokens)
            else:
                result = self._complete_stub(system, user, tier, schema, max_output_tokens)
        except ProviderUnavailable:
            raise
        except (APIStatusError, APIConnectionError, APITimeoutError, httpx.HTTPError) as exc:
            kind = _provider_failure_kind(exc)
            self._record_provider_failure("completion", kind)
            raise ProviderUnavailable(kind, service="completion") from exc
        self._record_provider_ready("completion")
        return result

    def _record_provider_ready(self, service: str) -> None:
        with self._health_lock:
            self._provider_health[service] = {"status": "ready", "reason": None}

    def _record_provider_failure(self, service: str, kind: str) -> None:
        with self._health_lock:
            self._provider_health[service] = {"status": "degraded", "reason": kind}

    def provider_status(self) -> dict[str, dict[str, str | None]]:
        with self._health_lock:
            return {service: dict(status) for service, status in self._provider_health.items()}

    def probe(self) -> bool:
        """Validate OpenAI-compatible credentials through the zero-token models endpoint.

        Failure degrades readiness but never prevents access to already-derived reading data.
        Anthropic lacks an equivalent zero-token endpoint, so it remains unchecked until its first call.
        """
        if self.provider == "stub":
            self._record_provider_ready("completion")
            self._record_provider_ready("embedding")
            return True
        if self.provider != "openai-compatible":
            return True
        try:
            self._oai.with_options(max_retries=0, timeout=5.0).models.list()
        except (APIStatusError, APIConnectionError, APITimeoutError, httpx.HTTPError) as exc:
            self._record_provider_failure("completion", _provider_failure_kind(exc))
            return False
        self._record_provider_ready("completion")
        return True

    @staticmethod
    def _usage_openai(resp):
        u = getattr(resp, "usage", None)
        return {"in": getattr(u, "prompt_tokens", 0) or 0, "out": getattr(u, "completion_tokens", 0) or 0}

    def _complete_openai(self, system, user, tier, schema, max_output_tokens):
        model = self._model_for(tier)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        cap_name = "max_completion_tokens" if self._is_native_openai else "max_tokens"
        cap = ({cap_name: max_output_tokens} if max_output_tokens is not None else {})
        if schema is None:
            resp = self._oai.chat.completions.create(model=model, messages=messages, **cap)
            return resp.choices[0].message.content or "", self._usage_openai(resp)
        if self._is_native_openai:
            resp = self._oai.chat.completions.parse(
                model=model, messages=messages, response_format=schema, **cap
            )
            parsed = resp.choices[0].message.parsed
            if parsed is None:
                raise ValueError("OpenAI returned no parsed structured output (refusal or truncation)")
            return parsed.model_dump(mode="json"), self._usage_openai(resp)
        return self._complete_generic_structured(model, messages, schema, max_output_tokens)

    def _complete_generic_structured(self, model, messages, schema, max_output_tokens):
        """Generic OpenAI-compatible structured output (D-A5). Try strict json_schema first; if the
        provider REJECTS that format at request time (many genuinely compatible endpoints —
        Groq/Together/llama.cpp/Ollama /v1 — don't support strict json_schema), DEGRADE to json_object
        with the schema in the prompt. In BOTH modes Pydantic-validate with one re-ask, failing CLOSED
        (raise) on persistently invalid output rather than fabricating — toward under-extraction, the
        safe direction."""
        strict = to_strict_json_schema(schema)
        rf = {"type": "json_schema",
              "json_schema": {"name": schema.__name__, "schema": strict, "strict": True}}
        # Probe strict support with the INITIAL request only. A request-time 400 there means the provider
        # rejects strict json_schema (Groq/Together/llama.cpp/Ollama /v1 …) -> degrade to json_object with
        # the schema in the prompt. We intentionally degrade on ANY initial 400 (a non-format 400 simply
        # 400s again under json_object and propagates — still fail-closed). A 400 on a LATER re-ask is NOT
        # a format signal, so it is left to propagate rather than triggering a second mode (pass-2 review).
        try:
            primed = self._oai.chat.completions.create(
                model=model, messages=messages, response_format=rf,
                **({"max_tokens": max_output_tokens} if max_output_tokens is not None else {}),
            )
        except BadRequestError:
            guided = list(messages) + [{"role": "system", "content":
                "Respond with ONE JSON object that conforms exactly to this JSON Schema:\n"
                + json.dumps(strict)}]
            return self._validate_loop(
                model, guided, schema, {"type": "json_object"},
                max_output_tokens=max_output_tokens,
            )
        return self._validate_loop(
            model, messages, schema, rf, primed=primed, max_output_tokens=max_output_tokens
        )

    def _validate_loop(self, model, messages, schema, response_format, primed=None,
                       max_output_tokens=None):
        """Pydantic-validate the structured reply; on failure append a corrective turn and re-ask
        exactly once; then fail closed. Shared by the strict-json_schema and json_object generic paths.
        `primed` lets the caller hand in an already-made first response (the strict-support probe) so it
        is validated without a duplicate request."""
        msgs = list(messages)
        last_err = None
        resp = primed
        total_usage = {"in": 0, "out": 0}
        for _ in range(2):                                # initial attempt + exactly one re-ask
            if resp is None:
                resp = self._oai.chat.completions.create(
                    model=model, messages=msgs, response_format=response_format,
                    **({"max_tokens": max_output_tokens}
                       if max_output_tokens is not None else {}),
                )
            content = resp.choices[0].message.content
            current_usage = self._usage_openai(resp)
            total_usage["in"] += current_usage["in"]
            total_usage["out"] += current_usage["out"]
            try:
                return (schema.model_validate_json(content or "").model_dump(mode="json"),
                        total_usage)
            except (ValidationError, ValueError, TypeError) as e:
                last_err = e
                msgs = msgs + [
                    {"role": "assistant", "content": content or ""},
                    {"role": "user", "content": f"Your previous output was invalid: {e}. Return ONLY "
                                                 f"valid JSON matching the schema; omit anything you "
                                                 f"are unsure about."}]
                resp = None                               # force a fresh request for the re-ask
        raise ValueError(f"generic provider returned schema-invalid output after re-ask: {last_err}")

    def _complete_anthropic(self, system, user, tier, schema, max_output_tokens):
        model = self._model_for(tier)
        body = {"model": model, "max_tokens": max_output_tokens or 4096, "system": system,
                "messages": [{"role": "user", "content": user}]}
        if schema is not None:                            # force a tool-call shaped to the strict schema
            body["tools"] = [{"name": "emit", "description": "emit the structured result",
                              "input_schema": to_strict_json_schema(schema)}]
            body["tool_choice"] = {"type": "tool", "name": "emit"}
        d = self._anthropic_post(body)
        u = d.get("usage", {})
        usage = {"in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0),
                 "cache_write": u.get("cache_creation_input_tokens", 0),
                 "cache_read": u.get("cache_read_input_tokens", 0)}
        content = d.get("content", [])                    # absent on a malformed 200 -> clean errors below
        if schema is not None:
            raw = next((c["input"] for c in content if c.get("type") == "tool_use"), None)
            if raw is None:
                raise ValueError("anthropic returned no tool_use block for the forced structured tool")
            return schema.model_validate(raw).model_dump(mode="json"), usage
        return "".join(c.get("text", "") for c in content if c.get("type") == "text"), usage

    def _anthropic_post(self, body):
        headers = {"x-api-key": self.anthropic_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        for attempt in range(self.max_retries + 1):
            r = self._http.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
            if r.status_code in _ANTHROPIC_RETRY_STATUS and attempt < self.max_retries:
                self._sleep(min(2 ** attempt, 8))         # bounded exponential backoff on 429/5xx
                continue
            r.raise_for_status()                          # 4xx, or a retryable status on the last try
            return r.json()

    def _complete_stub(self, system, user, tier, schema, max_output_tokens):
        """Deterministic offline backend: enough structure to exercise the pipeline without a network.
        NOT a quality oracle. With a schema, emits the extraction-shaped object and validates it
        THROUGH the schema (so the dict-of-strings contract + enum serialization are guaranteed).
        Both paths echo the INPUT CONTENT (the chapter text / the supplied facts) rather than the
        prompt scaffolding, so stub output is GROUNDED in its source by construction — the LIT-25
        sentence-grounding gate (and any realistic downstream check) treats it like model output,
        not instruction junk (a named LIT-25-era behavior change; nothing pinned the old prefixes)."""
        if schema is None:
            # a recap: echo the supplied-facts section of the synth prompt when present
            src = user.split("CHAPTER SUMMARIES:", 1)[-1] if "CHAPTER SUMMARIES:" in user else user
            return "[stub recap] " + " ".join(src.split())[:200], {"in": len(user) // 4, "out": 30}
        model_fields = getattr(schema, "model_fields", {})
        if "insufficient_evidence" in model_fields and "claims" in model_fields:
            # Offline mode cannot synthesize a grounded cited answer. Say that explicitly instead of
            # forcing the extraction-shaped fallback through the Ask schema and returning a 502.
            return (schema.model_validate({"insufficient_evidence": True, "claims": []})
                    .model_dump(mode="json"), {"in": len(user) // 4, "out": 5})
        if "insufficient_evidence" in model_fields:
            return (schema.model_validate({
                "insufficient_evidence": True, "text": None, "citation_ids": [],
            }).model_dump(mode="json"), {"in": len(user) // 4, "out": 5})
        if "references_future" in getattr(schema, "model_fields", {}):
            # the LIT-14 judge: the stub is not a real reviewer, and the stub recap is grounded in its
            # source by construction, so emit a benign (safe) verdict — keeps the offline recap pipeline
            # working without pretending to judge. Validated through the schema like every stub reply.
            return (schema.model_validate({"references_future": False, "unsupported_claims": []})
                    .model_dump(mode="json"), {"in": len(user) // 4, "out": 5})
        import re
        names = []
        for m in re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b", user):
            if m not in names and m.lower() not in ("chapter", "the project"):
                names.append(m)
        ents = [{"canonical_name": n, "type": "character", "aliases": [],
                 "matched_roster": False, "state": None} for n in names[:8]]
        body = user.split("CHAPTER TEXT:", 1)[-1] if "CHAPTER TEXT:" in user else user
        obj = {"chapter_summary": "[stub] " + " ".join(body.split())[:160], "entities": ents,
               "relationships": [], "events": [], "themes": []}
        return schema.model_validate(obj).model_dump(mode="json"), {"in": len(user) // 4, "out": 50}

    # ---- embeddings (routed on the INDEPENDENT embedding backend) -----------
    def embed(self, texts):
        if self._embed_oai is not None:
            try:
                resp = self._embed_oai.embeddings.create(model=self.embed_model, input=texts)
            except (APIStatusError, APIConnectionError, APITimeoutError, httpx.HTTPError) as exc:
                kind = _provider_failure_kind(exc)
                self._record_provider_failure("embedding", kind)
                raise ProviderUnavailable(kind, service="embedding") from exc
            # Realign by the SDK `index` — an OpenAI-compatible proxy may return data out of input order,
            # and a misaligned vector is a silent pin/KNN corruption (pass-1 review MEDIUM). But sort
            # ONLY when EVERY item carries an index: a proxy that OMITS index leaves it None, and a naive
            # sort would `None < None` -> crash; fall back to wire order there (pass-2 review regression).
            data = resp.data
            if data and all(d.index is not None for d in data):
                data = sorted(data, key=lambda d: d.index)
            vecs = [d.embedding for d in data]
            total = getattr(getattr(resp, "usage", None), "total_tokens", 0) or 0
            self._record_provider_ready("embedding")
            return vecs, {"in": total}
        # No real embeddings endpoint -> deterministic LEXICAL stub so the cosine RESOLUTION ALGORITHM
        # is exercised. WARN loudly (a configured-but-unreachable embedder is a silent-corruption trap)
        # and stamp the ACTUAL embedder ("stub:lexical-stub-256"), never the configured-but-unhonored
        # name — embed_identity() guarantees this.
        if self.embed_provider != "stub" and not self._warned_stub_embed:
            print(f"WARNING: embed provider {self.embed_provider!r} has no reachable endpoint/key — "
                  f"FALLING BACK to the lexical stub; these are NOT real embeddings and are stamped "
                  f"'stub:lexical-stub-256', not {self.embed_model!r}.", file=sys.stderr)
            self._warned_stub_embed = True
        self._record_provider_ready("embedding")
        return [_lexical_embed(t) for t in texts], {"in": 0}

    def embed_canary(self):
        """A fingerprint VECTOR of WHAT this embedder produces — embed a fixed probe and return the
        vector. Compared by COSINE with a tolerance (versioning.safe_swap), NOT exact equality, so it
        is robust to a real embedder's run-to-run numerical noise while still catching a genuine space
        change."""
        return self.embed(["__lit20_embed_canary__"])[0][0]


def _lexical_embed(text, dim=256):
    v = [0.0] * dim
    t = "".join(ch.lower() if ch.isalnum() else " " for ch in text)
    toks = t.split()
    grams = toks + [t[i:i + 3] for i in range(len(t) - 2)]
    for g in grams:
        # Stable feature bucketing only; this digest is not used for authentication or integrity.
        h = int(hashlib.md5(g.encode(), usedforsecurity=False).hexdigest(), 16)
        v[h % dim] += 1.0
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


def cosine(a, b):
    """Plain cosine similarity — the KNN distance the bookmark-bounded cast-roster resolver
    (``ingest/extraction/resolve.py``, LIT-6) imports from here, preserving the spike's
    ``from llm import cosine`` seam. (The DAL's ``BookmarkView``/``vectors.py`` keep their own ranker
    cosine for the spoiler funnel; this one is only for the resolution layer, never a fact read.)"""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
