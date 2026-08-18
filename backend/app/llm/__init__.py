"""LIT-20 — the one provider-agnostic LLM + embedding interface (client) plus the model-identity /
version-pinning / safe-swap policy (versioning). See ADR 0005 (interface) and ADR 0007 D-A5 (the
production re-port: Pydantic schema source of truth, OpenAI native strict structured outputs, dict
downstream)."""

from app.llm.client import LLMClient, cosine, detect_provider
from app.llm import versioning

__all__ = ["LLMClient", "cosine", "detect_provider", "versioning"]
