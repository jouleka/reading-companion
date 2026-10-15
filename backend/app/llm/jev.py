"""Typed TypeSafe transport. No generation, redirects, automatic retries or plaintext key files."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import sys

import httpx


class JevUnavailable(RuntimeError):
    """Public-safe failure: request bodies, credentials and provider errors stay private."""


def windows_credential() -> str:
    """Read the existing current-user PowerShell SecureString DPAPI blob in memory only."""
    if sys.platform != "win32":
        raise JevUnavailable("Windows Jev credential is unavailable")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    try:
        path = Path(os.environ["LOCALAPPDATA"]) / "Codex/Secrets/TypeSafeJev/api-key.dpapi"
        encrypted = bytes.fromhex(path.read_text(encoding="utf-8-sig").strip())
        raw = (ctypes.c_ubyte * len(encrypted)).from_buffer_copy(encrypted)
        source, target = Blob(len(encrypted), raw), Blob()
        crypt = ctypes.windll.crypt32
        crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p,
                                             ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.c_ulong, ctypes.POINTER(Blob)]
        crypt.CryptUnprotectData.restype = ctypes.c_int
        if not crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1,
                                       ctypes.byref(target)):
            raise ValueError("DPAPI failure")
        try:
            key = ctypes.string_at(target.data, target.size).decode("utf-16-le")
        finally:
            ctypes.memset(target.data, 0, target.size)
            free = ctypes.windll.kernel32.LocalFree
            free.argtypes, free.restype = [ctypes.c_void_p], ctypes.c_void_p
            free(target.data)
        if not key.strip():
            raise ValueError("empty credential")
        return key
    except Exception:
        raise JevUnavailable("Stored Jev credential could not be loaded") from None


class JevClient:
    def __init__(self, *, api_key, model="jev-1.13.0", timeout=5.0, transport=None):
        self.model = model
        self.status = "configured"
        self._http = httpx.Client(
            base_url="https://api.typesafe.ai", timeout=timeout, follow_redirects=False,
            headers={"Authorization": f"Bearer {api_key}"}, transport=transport,
        )

    def close(self):
        self._http.close()

    def evaluate(self, state, questions):
        if not questions or len(questions) > 24:
            raise JevUnavailable("Jev question batch is outside the bounded contract")
        body = {"model": self.model, "state": state, "questions": questions}
        try:
            with self._http.stream("POST", "/v1/systemone", json=body) as response:
                if response.status_code != 200:
                    raise ValueError("provider status")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 262144:
                        raise ValueError("oversized response")
                    chunks.append(chunk)
                value = json.loads(b"".join(chunks))
            if value.get("model") != self.model or set(value.get("answers", {})) != set(questions):
                raise ValueError("model or answer coverage")
            for name, question in questions.items():
                answer = value["answers"][name]
                options = set(question["criteria"])
                probabilities = answer.get("probabilities", {})
                if (answer.get("type") != "choice" or answer.get("choice") not in options
                        or set(probabilities) != options
                        or any(type(p) not in (int, float) or not 0 <= p <= 1
                               for p in probabilities.values())
                        or abs(sum(probabilities.values()) - 1) > 0.02
                        or type(answer.get("confidence")) not in (int, float)
                        or not 0 <= answer["confidence"] <= 1
                        or probabilities[answer["choice"]] < max(probabilities.values()) - 1e-6):
                    raise ValueError("invalid typed answer")
            usage = value.get("usage", {})
            if any(type(usage.get(k)) is not int or usage[k] < 0
                   for k in ("input_tokens", "output_tokens")) or usage["input_tokens"] == 0:
                raise ValueError("invalid usage")
            self.status = "connected"
            return value["answers"], {"in": usage["input_tokens"], "out": usage["output_tokens"]}
        except Exception:
            self.status = "fallback"
            raise JevUnavailable("Jev evaluation unavailable or invalid") from None
