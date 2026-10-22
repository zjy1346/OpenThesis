"""Model-stage seam: material admission, exact request size and call lifetime.

This module produces candidates only. The graph executor persists them before
the named business validator can admit content. It does not read storage,
assemble reports or invent missing materials. The bounded rate-limit policy is
the legacy initial-call adapter; recovery actions use a single attempt so the
durable RecoveryCoordinator owns their retries and incident budget.
"""
from __future__ import annotations

import hashlib
import math
import re
import threading
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable

from ..model_scheduler import ModelRequestScheduler
from ..providers import ModelProvider, ProviderError
from ..research_materials import insufficient_material_result
from ..resilience.contracts import plain_json_value
from .model_format import proven_format_rejection, proven_rate_rejection, rejection_retry_after
from .model_output import (
    ModelOutputContract, field_repair_prompt, output_repair_prompt,
    repairable_fields, repairable_output, normalize_stage_output,
    response_refused,
)
from ..research_claim_repair import ClaimRepairContract


class ModelStageCancelled(RuntimeError):
    pass


def _repair_failure_code(error: Exception) -> str:
    if isinstance(error, ModelStageCancelled) or getattr(error, "code", "") == "MODEL_CANCELLED":
        raise error
    code = getattr(error, "code", "")
    return code if isinstance(code, str) and re.fullmatch(r"(?:MODEL|RECOVERY)_[A-Z0-9_]{1,70}", code) else "MODEL_REPAIR_LOCAL_PROCESSING_FAILED"


def _incomplete_output(candidate: Any) -> bool:
    if response_refused(candidate) or isinstance(candidate, dict) and candidate.get("_response_error") == "provider_refusal":
        return False
    meta = candidate.get("_response_meta", {}) if isinstance(candidate, dict) else {}
    return isinstance(meta, dict) and str(meta.get("finish_reason") or "").casefold() in {
        "length", "max_tokens", "max_output_tokens"}


def _mark_incomplete_output(candidate: dict[str, Any]) -> dict[str, Any]:
    return {**candidate, "_output_incomplete": {"code": "MODEL_OUTPUT_BUDGET_EXHAUSTED",
                                              "state": "pending"}}


@dataclass(frozen=True, slots=True)
class ProviderContextCapability:
    """Shape threshold; never authorization to truncate source material."""

    max_input_tokens: int = 128_000
    reserved_output_tokens: int = 4_096
    reserved_repair_tokens: int = 2_048
    explicit_max_input_bytes: int | None = None
    bytes_per_token: float = 1.0

    @property
    def available_input_tokens(self) -> int:
        return max(1, self.max_input_tokens - self.reserved_output_tokens - self.reserved_repair_tokens)

    @property
    def max_input_bytes(self) -> int:
        if self.explicit_max_input_bytes is not None:
            return max(1, int(self.explicit_max_input_bytes))
        return max(1, int(self.available_input_tokens * self.bytes_per_token))


class SynthesisContextLimitError(RuntimeError):
    """Complete material cannot fit the configured request shape."""

    code = "MODEL_CONTEXT_CAPACITY"
    retryable = False  # Requires partitioning/changed capacity, not identical replay.

    def __init__(self, sections: list[dict[str, Any]], limit_bytes: int, *,
                 required_bytes: int | None = None,
                 counting_mode: str = "estimated_utf8_shape"):
        super().__init__("synthesis context exceeds provider input budget")
        self.sections = sections
        self.limit_bytes = limit_bytes
        measured = [item.get("bytes") for item in sections if isinstance(item, dict)]
        measured = [value for value in measured if isinstance(value, int)
                    and not isinstance(value, bool) and value > limit_bytes]
        self.required_bytes = (required_bytes if required_bytes is not None
                               else max(measured, default=limit_bytes + 1))
        self.available_bytes = limit_bytes
        self.counting_mode = counting_mode


def _positive_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def provider_context_capability(provider: Any) -> ProviderContextCapability:
    explicit = getattr(provider, "max_input_bytes", None)
    tokens = next((getattr(provider, name) for name in
                   ("context_window_tokens", "max_input_tokens")
                   if _positive_integer(getattr(provider, name, None))), 128_000)
    reserves = []
    for name, fallback in (("reserved_output_tokens", 4_096), ("reserved_repair_tokens", 2_048)):
        value = getattr(provider, name, fallback)
        reserves.append(value if isinstance(value, int) and not isinstance(value, bool)
                        and value >= 0 else fallback)
    ratio = getattr(provider, "input_bytes_per_token", 1.0)
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not math.isfinite(ratio) or ratio <= 0:
        ratio = 1.0
    return ProviderContextCapability(tokens, *reserves,
                                     explicit if _positive_integer(explicit) else None, float(ratio))


def provider_declares_context_capability(provider: Any) -> bool:
    return any(_positive_integer(getattr(provider, name, None)) for name in
               ("context_window_tokens", "max_input_tokens", "max_input_bytes"))


@dataclass(frozen=True, slots=True)
class ModelStageRequest:
    stage_id: str
    system_prompt: str
    user_prompt: str
    json_mode: bool = True
    max_input_bytes: int | None = None
    output_contract: ModelOutputContract | None = None
    claim_repair_contract: ClaimRepairContract | None = None
    purpose: str = "generation"
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        if (self.purpose not in {"generation", "semantic_verification", "material_coverage", "section_delivery"}
                or not isinstance(self.stage_id, str) or not self.stage_id.strip()
                or not isinstance(self.system_prompt, str)
                or not isinstance(self.user_prompt, str)
                or not isinstance(self.json_mode, bool)
                or (self.max_input_bytes is not None and not _positive_integer(self.max_input_bytes))
                or (self.max_output_tokens is not None and not _positive_integer(self.max_output_tokens))
                or (self.output_contract is not None and not isinstance(self.output_contract, ModelOutputContract))
                or (self.claim_repair_contract is not None and not isinstance(self.claim_repair_contract, ClaimRepairContract))):
            raise ValueError("MODEL_STAGE_REQUEST_INVALID")

    @property
    def input_bytes(self) -> int:
        return len(self.system_prompt.encode("utf-8")) + len(self.user_prompt.encode("utf-8"))

    @property
    def input_digest(self) -> str:
        # Length prefixes distinguish ambiguous concatenations, without putting
        # raw prompts into public diagnostics or the recovery journal.
        parts = (self.stage_id, self.system_prompt, self.user_prompt, str(self.json_mode))
        if self.max_output_tokens is not None:
            parts += (f"output-budget:{self.max_output_tokens}",)
        if self.purpose in {"material_coverage", "section_delivery"}:
            parts += (f"purpose:{self.purpose}",)
        encoded = b"".join(len(value.encode("utf-8")).to_bytes(8, "big")
                           + value.encode("utf-8") for value in parts)
        return hashlib.sha256(encoded).hexdigest()


class ModelStageExecutor:
    def __init__(self, provider: ModelProvider, scheduler: ModelRequestScheduler, *,
                 cancel_check: Callable[[], bool] | None = None, request_journal: Any = None):
        self.provider = provider
        self.scheduler = scheduler
        self.cancel_check = cancel_check or (lambda: False)
        self.request_journal = request_journal

    def execute(self, request: ModelStageRequest, *,
                material_adequacy: dict[str, Any] | None = None,
                language: str = "zh-CN", single_attempt: bool = False) -> dict[str, Any]:
        """Return a detached candidate; a returned dict is not verified content."""
        if self.cancel_check():
            raise ModelStageCancelled()
        gate = deepcopy(material_adequacy) if material_adequacy is not None else None
        if gate is not None and gate.get("status") == "insufficient":
            return insufficient_material_result(gate, language)
        from .input_budget import input_measurement
        measurement = input_measurement(self.provider, request.system_prompt, request.user_prompt)
        budget_provider = self.provider
        if request.max_output_tokens is not None and callable(getattr(budget_provider, "for_output_budget", None)):
            budget_provider = budget_provider.for_output_budget(request.max_output_tokens)
        capability = provider_context_capability(budget_provider)
        input_exceeded = request.max_input_bytes is not None and request.input_bytes > request.max_input_bytes
        if measurement["exact"] and provider_declares_context_capability(self.provider):
            input_exceeded = measurement["tokens"] > capability.available_input_tokens
            if capability.explicit_max_input_bytes is not None:
                input_exceeded = input_exceeded or request.input_bytes > capability.explicit_max_input_bytes
        if input_exceeded:
            raise SynthesisContextLimitError(
                [{"name": request.stage_id, "bytes": request.input_bytes}],
                request.max_input_bytes or capability.max_input_bytes, required_bytes=request.input_bytes,
                counting_mode=measurement["method"],
            )
        result: dict[str, Any] | None = None
        error: BaseException | None = None
        request_scope = self.request_journal.capture_scope() if self.request_journal is not None else None

        def invoke() -> None:
            nonlocal result, error, request
            retries, wait_total = 0, 0.0
            try:
                while True:
                    if self.cancel_check():
                        raise ModelStageCancelled()
                    try:
                        def scheduled_call(json_mode, *, format_compensation=False, output_repair=False,
                                           field_patch=(), claim_plan=None, transient_retry=False):
                            provider = self.provider
                            if request.max_output_tokens is not None and callable(getattr(provider, "for_output_budget", None)):
                                provider = provider.for_output_budget(request.max_output_tokens)
                            context = self.request_journal.transport_context() if self.request_journal is not None else None
                            if context is not None:
                                config = getattr(provider, "config", None)
                                self.request_journal.store.model_activity.describe(context,
                                    {"configured_model_id": getattr(config, "configured_model_id", "")})
                            if context is not None and callable(getattr(provider, "for_model_events", None)):
                                provider = provider.for_model_events(context,
                                    lambda event: self.request_journal.store.model_activity.update(context, event),
                                    lambda: self.request_journal.store.model_activity.is_cancel_requested(context))
                            def cancel_wait():
                                if (context is not None and self.request_journal.store.model_activity.is_cancel_requested(context)):
                                    # This callback runs before a slot is acquired, and again
                                    # immediately before dispatch. No remote wire exists yet.
                                    raise ProviderError("Queued model call cancelled locally",
                                                        code="MODEL_CALL_CANCELLED_BEFORE_SEND")
                                return self.cancel_check()
                            with self.scheduler.slot(cancel_wait):
                                if self.cancel_check():
                                    raise ProviderError("Research cancelled before dispatch", code="MODEL_CALL_CANCELLED_BEFORE_SEND")
                                cancel_wait()
                                try:
                                    effective_prompt = (request.claim_repair_contract.prompt(request.user_prompt, candidate, claim_plan)
                                        if claim_plan is not None else
                                        field_repair_prompt(request.user_prompt, request.output_contract, field_patch) if field_patch else
                                        output_repair_prompt(request.user_prompt) if output_repair else request.user_prompt)
                                    if self.request_journal is not None:
                                        self.request_journal.record_wire_diagnostics(request.system_prompt, effective_prompt,
                                            provider=provider, purpose="claim_repair" if claim_plan is not None else
                                            "field_repair" if field_patch else "output_repair" if output_repair else
                                            "format_repair" if format_compensation else "transport_retry" if transient_retry
                                            else request.purpose,
                                            target_paths=tuple(target.path for target in claim_plan.targets)
                                                if claim_plan is not None else field_patch)
                                        dispatch = getattr(self.request_journal, "before_dispatch", None)
                                        if callable(dispatch):
                                            dispatch()
                                    if format_compensation or output_repair or field_patch or claim_plan is not None:
                                        response = provider.generate_once(
                                            request.system_prompt,
                                            effective_prompt,
                                            json_mode=json_mode,
                                        )
                                    else:
                                        response = provider.generate(
                                            request.system_prompt,
                                            request.user_prompt,
                                            json_mode=json_mode,
                                        )
                                    if self.request_journal is not None:
                                        try:
                                            self.request_journal.finish_wire_diagnostics(response=response)
                                            self.request_journal.hydrate_input_budget(provider)
                                        except Exception:
                                            # Diagnostics cannot turn a received paid
                                            # body into an unknown submission. The
                                            # main journal still persists its receipt.
                                            pass
                                    return response
                                except BaseException as exc:
                                    if self.request_journal is not None:
                                        try:
                                            self.request_journal.finish_wire_diagnostics(error=exc)
                                        except Exception:
                                            pass
                                    if isinstance(exc, ProviderError) and proven_rate_rejection(exc):
                                        self.scheduler.mark_rate_limited(rejection_retry_after(exc))
                                    raise

                        try:
                            def call_provider():
                                return scheduled_call(request.json_mode)
                            candidate = (self.request_journal.execute(request, call_provider, request_scope)
                                         if self.request_journal is not None else call_provider())
                        except ProviderError as exc:
                            generate_once = getattr(self.provider, "generate_once", None)
                            if self.cancel_check() or exc.code == "MODEL_CANCELLED":
                                raise ModelStageCancelled() from exc
                            if self.request_journal is not None and not single_attempt and proven_rate_rejection(exc):
                                candidate = self.request_journal.recover_transient(request,
                                    lambda: scheduled_call(request.json_mode, transient_retry=True), request_scope)
                            elif (self.request_journal is not None and not single_attempt and request.json_mode
                                  and callable(generate_once) and proven_format_rejection(exc)):
                                candidate = self.request_journal.recover_format(request,
                                    lambda: scheduled_call(False, format_compensation=True), request_scope)
                            else:
                                raise
                        if _incomplete_output(candidate):
                            # Length is not a new research gap or permission to
                            # resend all sources at successively larger limits.
                            # Closed objects survive; the host can request only
                            # a specific missing object under the shared budget.
                            candidate = _mark_incomplete_output(candidate)
                        candidate = normalize_stage_output(candidate, request.output_contract)
                        if (self.request_journal is not None and not single_attempt and request.json_mode
                                and callable(getattr(self.provider, "generate_once", None))
                                and not _incomplete_output(candidate)
                                and repairable_output(candidate)):
                            # Local JSON recovery has already retained closed
                            # values. Without a safe target boundary, protocol
                            # damage cannot justify regenerating all research.
                            candidate = {**candidate, "_output_repair": {
                                "state": "pending", "code": "MODEL_OUTPUT_NO_SAFE_CONTINUATION"}}
                        fields = ()
                        try:
                            fields = repairable_fields(candidate, request.output_contract)
                        except Exception as repair_error:
                            candidate = {**candidate, "_field_repair": {
                                "state": "pending", "code": _repair_failure_code(repair_error)}}
                        if (fields and self.request_journal is not None and not single_attempt
                                and request.json_mode and not _incomplete_output(candidate)
                                and callable(getattr(self.provider, "generate_once", None))):
                            try:
                                repair_bytes = (len(request.system_prompt.encode("utf-8"))
                                    + len(field_repair_prompt(request.user_prompt, request.output_contract,
                                                            fields).encode("utf-8")))
                                if request.max_input_bytes is not None and repair_bytes > request.max_input_bytes:
                                    raise ProviderError("Complete field repair needs a larger model context",
                                                        code="MODEL_FIELD_REPAIR_CONTEXT_REQUIRED")
                                candidate = self.request_journal.recover_fields(request,
                                    lambda: scheduled_call(request.json_mode, field_patch=fields), request_scope)
                            except Exception as repair_error:
                                candidate = {**candidate, "_field_repair": {
                                    "state": "pending", "code": _repair_failure_code(repair_error), "targets": list(fields)}}
                        candidate = normalize_stage_output(candidate, request.output_contract)
                        claim_plan = None
                        try:
                            claim_plan = (request.claim_repair_contract.plan(candidate)
                                          if request.claim_repair_contract is not None else None)
                            if (claim_plan is not None and self.request_journal is not None and not single_attempt
                                    and not _incomplete_output(candidate)
                                    and callable(getattr(self.provider, "generate_once", None))):
                                repair_prompt = request.claim_repair_contract.prompt(request.user_prompt, candidate, claim_plan)
                                repair_bytes = len(request.system_prompt.encode("utf-8")) + len(repair_prompt.encode("utf-8"))
                                if request.max_input_bytes is None or repair_bytes <= request.max_input_bytes:
                                    candidate = self.request_journal.recover_claims(request,
                                        lambda: scheduled_call(request.json_mode, claim_plan=claim_plan), request_scope)
                                else:
                                    candidate = {**candidate, "_claim_repair": {
                                        "state": "pending", "code": "MODEL_CLAIM_REPAIR_CONTEXT_REQUIRED",
                                        "candidate_digest": claim_plan.candidate_digest}}
                        except Exception as repair_error:
                            candidate = {**candidate, "_claim_repair": {
                                "state": "pending", "code": _repair_failure_code(repair_error),
                                **({"candidate_digest": claim_plan.candidate_digest} if claim_plan is not None else {})}}
                        if self.cancel_check():
                            raise ModelStageCancelled()
                        if not isinstance(candidate, dict):
                            raise ValueError("MODEL_STAGE_CANDIDATE_INVALID")
                        # The request journal has already preserved the full
                        # host receipt. Stages consume ordinary JSON values,
                        # never transport dict subclasses with constructors.
                        result = plain_json_value(candidate)
                        self.scheduler.mark_success()
                        return
                    except ProviderError as exc:
                        if self.cancel_check() or exc.code == "MODEL_CANCELLED":
                            raise ModelStageCancelled() from exc
                        if exc.code != "MODEL_RATE_LIMITED":
                            raise
                        try:
                            delay = float(exc.retry_after_seconds) if exc.retry_after_seconds is not None else 30.0
                        except (TypeError, ValueError):
                            delay = 30.0
                        if not math.isfinite(delay):
                            delay = 30.0
                        delay = max(1.0, delay)
                        if not proven_rate_rejection(exc):
                            self.scheduler.mark_rate_limited(delay)
                        if self.request_journal is not None or single_attempt or retries >= 2 or wait_total + delay > 600.0:
                            raise
                        retries += 1
                        wait_total += delay
            except BaseException as exc:
                # A worker's failure must reach the caller; it must never be
                # mistaken for an empty successful provider response.
                error = exc

        worker = threading.Thread(target=invoke, daemon=True, name="openthesis-model-stage")
        worker.start()
        while worker.is_alive():
            worker.join(0.05)
            if self.cancel_check() and self.request_journal is None:
                raise ModelStageCancelled()
            # A journaled request owns persistent intent/outcome writes. The
            # RPC cancellation acknowledgement is immediate, but its job must
            # remain cancelling until this worker has closed those resources.
            # Detaching here lets a late journal write race a terminal status,
            # a new recovery, shutdown or database backup/replacement.
            # Unjournaled adapters retain their prompt cancellation behavior.
        if self.cancel_check():
            raise ModelStageCancelled()
        if error is not None:
            raise error
        if result is None:
            raise RuntimeError("MODEL_STAGE_NO_CANDIDATE")
        if gate is not None:
            result["_material_adequacy"] = gate
        return result
