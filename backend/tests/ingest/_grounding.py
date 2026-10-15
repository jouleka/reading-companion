"""Explicit canned extraction reviews for orchestration fixtures, never semantic quality evidence.

The real validator requires provider-supplied evidence coverage. These helpers deliberately fabricate
an accepting audit for tests that exercise a DIFFERENT contract (atomicity, errors, cost or wiring).
Grounding regressions must set their disputed fragments/evidence explicitly instead.
"""
import json


def review_payload(user):
    return json.loads(user.split("(untrusted JSON data):\n", 1)[1])


def reviewed_source(user):
    return "".join(part["text"] for part in review_payload(user)["source_passages"])


def canned_grounding_verdict(user, *, unsupported_claims=(), references_future=False):
    payload = review_payload(user)
    chapter_ids = [part["id"] for part in payload["source_passages"] if part["text"].strip()]
    audits = []
    for field in payload["fields"]:
        sources = chapter_ids[:1]
        if not sources and field["kind"] == "identity" and payload["prior_visible_identities"]:
            sources = [-1]
        audits.append({"field_id": field["id"], "assertions": [{
            "segment_id": segment["id"], "source_ids": sources,
            "support": "supported" if sources else "unsupported",
        } for segment in field["segments"]]})
    return {"references_future": references_future,
            "unsupported_claims": list(unsupported_claims), "field_audits": audits}
