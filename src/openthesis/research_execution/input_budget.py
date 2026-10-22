"""Local input accounting shared by planning, dispatch and diagnostics.

An estimate is never a provider guarantee. Calibrations use received usage only,
are route-bound, and survive scoped provider copies. No text or credentials are
retained. Optional tokenizers must already be available locally.
"""
from __future__ import annotations

import math
import threading
from typing import Any, Callable


class InputBudgetState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._route: tuple[str, str, str] | None = None
        self._ratio: float | None = None
        self._counter: Callable[[str, str], int] | None = None
        self._tokenizer = "unavailable"

    @property
    def bytes_per_token(self) -> float:
        with self._lock:
            # Conservative *planning* fallback, not a claimed universal bound.
            return self._ratio if self._ratio is not None else 1.0

    def observe(self, *, input_bytes: int, input_tokens: int, provider_id: str,
                model_id: str, endpoint_host: str) -> None:
        if (type(input_bytes) is not int or type(input_tokens) is not int
                or input_bytes < 4096 or input_tokens < 512
                or input_bytes > 16 * 1024 * 1024 or input_tokens > 2_147_483_647
                or not all(isinstance(value, str) and value.strip()
                           for value in (provider_id, model_id, endpoint_host))):
            return
        route = (provider_id, model_id, endpoint_host)
        with self._lock:
            if self._route != route:
                self._route, self._ratio = route, None
                self._counter, self._tokenizer = locally_available_counter(provider_id, model_id)
            ratio = min(8.0, input_bytes / input_tokens * 0.9)
            self._ratio = min(self._ratio, ratio) if self._ratio is not None else ratio

    def measure(self, system: str, user: str) -> dict[str, Any]:
        size = len(system.encode("utf-8")) + len(user.encode("utf-8"))
        with self._lock:
            counter, tokenizer, ratio = self._counter, self._tokenizer, self.bytes_per_token
            calibrated = self._ratio is not None
        if counter is not None:
            try:
                count = counter(system, user)
                if type(count) is int and count >= 0:
                    return {"tokens": count, "method": "provider_tokenizer", "tokenizer": tokenizer,
                            "input_bytes": size, "exact": True}
            except Exception:
                pass
        return {"tokens": math.ceil(size / ratio) + 256, "method":
                "observed_usage_calibrated_estimate" if calibrated else "utf8_bytes_estimate",
                "tokenizer": "unavailable", "input_bytes": size, "exact": False}


def locally_available_counter(provider_id: str, model_id: str):
    """Reuse a matching, already loaded OpenAI tokenizer without any I/O.

    Unknown models/routes remain estimates. Never download tokenizer assets as
    a side effect of research, or apply an OpenAI tokenizer to another vendor.
    Chat envelope overhead is explicitly included; server-added content is not
    observable, so this remains a local tokenizer measurement, not a billing receipt.
    """
    if provider_id != "openai":
        return None, "unavailable"
    try:
        import tiktoken
        name = tiktoken.model.encoding_name_for_model(model_id)
        # get_encoding may download or replace a corrupt cache file. The
        # research loop must do neither; the host can load its tokenizer ahead
        # of time. An installed-but-unloaded tokenizer remains unavailable.
        encoding = tiktoken.registry.ENCODINGS.get(name)
        if encoding is None:
            return None, "unavailable"
        return (lambda system, user: len(encoding.encode(system, disallowed_special=()))
                + len(encoding.encode(user, disallowed_special=())) + 16), f"tiktoken:{name}"
    except (ImportError, KeyError, AttributeError, ValueError, OSError):
        return None, "unavailable"


def input_measurement(provider: Any, system: str, user: str) -> dict[str, Any]:
    measure = getattr(provider, "input_token_measurement", None)
    if callable(measure):
        return measure(system, user)
    count = getattr(provider, "count_input_tokens", None)
    tokenizer = getattr(provider, "tokenizer_name", "unavailable")
    if callable(count) and tokenizer != "unavailable":
        try:
            tokens = count(system, user)
            if type(tokens) is int and tokens >= 0:
                return {"tokens": tokens, "method": "provider_tokenizer", "tokenizer": tokenizer,
                        "exact": True, "input_bytes": len(system.encode()) + len(user.encode())}
        except Exception:
            pass
    return InputBudgetState().measure(system, user)
