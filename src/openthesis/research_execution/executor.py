"""One dependency executor for first execution and checkpoint continuation.

Handlers produce candidates; validators own trusted content. This module has
no prompt/provider, financial, renderer or SQL dependency. Durable repositories
are injected at the candidate/completion seams so no stale response can publish
merely because its worker returned successfully.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
from dataclasses import dataclass, field, replace
import re
from types import MappingProxyType
from typing import Any, Callable, Mapping

from ..resilience.contracts import digest, plain_json_value
from .registry import StageRegistry, StageSpec


class StageExecutionCancelled(RuntimeError):
    pass


def dependency_findings(value: Any) -> Any:
    """The actual downstream input, excluding host proof/prompt history.

    Proof records stay in their stage checkpoint. Including their changing
    local audit hashes in producer input makes identical business findings
    look new and repeats already completed calls after local revalidation.
    """
    if isinstance(value, dict):
        return {key: dependency_findings(child) for key, child in value.items()
                if (not str(key).startswith("_") or key == "_verification_state")
                and key not in {"trusted_channels", "prior_artifacts", "raw_response", "prompt", "journal"}}
    if isinstance(value, (list, tuple)):
        return [dependency_findings(child) for child in value]
    return deepcopy(value)


def stage_input_digest(spec: StageSpec, input_identity: str, fingerprint: str,
                       dependencies: Mapping[str, "StageResult"]) -> str:
    """One input identity for both planning and executing persisted stages."""
    return digest({"input": input_identity, "stage": spec.identity,
                   "execution_fingerprint": fingerprint,
                   "dependencies": {
                       key: {"stage_identity": dependencies[key].stage_identity,
                             "validation_state": dependencies[key].validation.state,
                             "issue_codes": dependencies[key].validation.issues,
                             "trusted_output_sha256": digest(dependency_findings(dependencies[key].validation.trusted_output))}
                       for key in spec.depends_on}})


@dataclass(frozen=True, slots=True)
class StageValidation:
    passed: bool
    trusted_output: dict[str, Any] = field(default_factory=dict)
    state: str = "failed_verification"
    issues: tuple[str, ...] = ()
    record: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        states = {"completed_verified", "completed_partial", "failed_verification", "needs_action"}
        if self.state not in states or self.passed != (self.state == "completed_verified"):
            raise ValueError("STAGE_VALIDATION_STATE_INCONSISTENT")
        if self.state in {"failed_verification", "needs_action"} and self.trusted_output:
            raise ValueError("UNVERIFIED_STAGE_CONTENT_CANNOT_BECOME_TRUSTED")


@dataclass(frozen=True, slots=True)
class StageResult:
    stage_id: str
    stage_identity: str
    input_digest: str
    candidate: dict[str, Any]
    validation: StageValidation
    candidate_digest: str
    error_code: str = ""
    retryable: bool = False
    execution_fingerprint: str = ""
    has_candidate: bool = False
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.validation.state in {"completed_verified", "completed_partial"}


class StageExecutor:
    """Execute a registered graph; resume reuses only identical validated input."""

    def __init__(
        self, handlers: Mapping[str, Callable[..., dict[str, Any]]],
        validators: Mapping[str, Callable[..., StageValidation]], *,
        checkpoint: Callable[[StageResult], None] | None = None,
        candidate_checkpoint: Callable[[StageSpec, str, dict[str, Any]], None] | None = None,
        candidate_preparer: Callable[..., None] | None = None,
        candidate_verifier: Callable[..., StageResult] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        progress: Callable[[str, str], None] | None = None,
    ):
        self.handlers = MappingProxyType(dict(handlers))
        self.validators = MappingProxyType(dict(validators))
        self.checkpoint = checkpoint or (lambda _result: None)
        self.candidate_checkpoint = candidate_checkpoint or (lambda _spec, _digest, _candidate: None)
        self.candidate_preparer = candidate_preparer
        self.candidate_verifier = candidate_verifier
        self.cancel_check = cancel_check or (lambda: False)
        self.progress = progress or (lambda _stage, _state: None)

    def _check_cancelled(self) -> None:
        if self.cancel_check():
            raise StageExecutionCancelled()

    def execute(
        self, registry: StageRegistry, inputs: dict[str, Any], *,
        previous: Mapping[str, StageResult] | None = None,
        targets: tuple[str, ...] | None = None,
        invalidated: tuple[str, ...] = (), max_workers: int = 1,
        execution_fingerprint: str = "",
        execution_fingerprints: Mapping[str, str] | None = None,
        propagate_invalidation: bool = True,
        local_only: bool = False,
        revalidation_candidates: Mapping[str, StageResult] | None = None,
        stage_inputs: Mapping[str, dict[str, Any]] | None = None,
    ) -> dict[str, StageResult]:
        if type(local_only) is not bool or isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 8:
            raise ValueError("STAGE_WORKER_LIMIT_INVALID")
        if not isinstance(execution_fingerprint, str):
            raise ValueError("STAGE_EXECUTION_FINGERPRINT_INVALID")
        fingerprints = dict(execution_fingerprints or {})
        if (set(fingerprints) - {spec.stage_id for spec in registry.stages}
                or any(not isinstance(value, str) or not value for value in fingerprints.values())
                or not isinstance(propagate_invalidation, bool)):
            raise ValueError("STAGE_EXECUTION_FINGERPRINT_INVALID")
        for spec in registry.stages:
            if spec.handler_id not in self.handlers or spec.validator_id not in self.validators:
                raise ValueError("STAGE_IMPLEMENTATION_NOT_REGISTERED")
        frozen_inputs = deepcopy(inputs)
        scoped_inputs = deepcopy(dict(stage_inputs or {}))
        if (set(scoped_inputs) - {spec.stage_id for spec in registry.stages}
                or any(not isinstance(value, dict) for value in scoped_inputs.values())):
            raise ValueError("STAGE_INPUT_SCOPE_INVALID")
        input_identities = {spec.stage_id: digest(scoped_inputs.get(spec.stage_id, frozen_inputs))
                            for spec in registry.stages}
        selected = set(registry.ancestors(targets)) if targets is not None else {spec.stage_id for spec in registry.stages}
        invalid = set(registry.descendants(invalidated)) if propagate_invalidation else set(invalidated)
        for stage_id in invalid:
            registry.get(stage_id)
        remaining = set(selected)
        results: dict[str, StageResult] = {}
        saved = dict(previous or {})
        local_candidates = dict(revalidation_candidates or {})
        if execution_fingerprints is None and invalidated:
            for spec in registry.stages:
                prior = saved.get(spec.stage_id)
                if (spec.stage_id not in invalid and prior is not None
                        and prior.execution_fingerprint and prior.stage_identity == spec.identity
                        and all(key in saved for key in spec.depends_on)
                        and prior.input_digest == stage_input_digest(
                            spec, input_identities[spec.stage_id], prior.execution_fingerprint, saved)):
                    fingerprints[spec.stage_id] = prior.execution_fingerprint
        executor = ThreadPoolExecutor(max_workers=max_workers)
        pending = {}

        def expected(spec: StageSpec) -> str:
            return stage_input_digest(spec, input_identities[spec.stage_id],
                fingerprints.get(spec.stage_id, execution_fingerprint), results)

        def verify_local(spec: StageSpec, stage_input: str, candidate: dict[str, Any],
                   fingerprint: str) -> StageResult:
            frozen_candidate = plain_json_value(candidate)
            candidate_hash = digest(frozen_candidate)
            verification_copy = deepcopy(frozen_candidate)
            self._check_cancelled()
            self.progress(spec.stage_id, "validating")
            try:
                validation = self.validators[spec.validator_id](spec, verification_copy,
                    deepcopy(scoped_inputs.get(spec.stage_id, frozen_inputs)))
                if not isinstance(validation, StageValidation) or digest(verification_copy) != candidate_hash:
                    raise ValueError("STAGE_VALIDATOR_CONTRACT_INVALID")
                validation = deepcopy(validation)
                response_meta = frozen_candidate.get("_response_meta", {})
                incomplete = (isinstance(frozen_candidate.get("_output_incomplete"), dict)
                    or isinstance(response_meta, dict) and str(response_meta.get("finish_reason") or "").casefold()
                       in {"length", "max_tokens", "max_output_tokens"})
                if (incomplete
                        and validation.state in {"completed_verified", "completed_partial"}):
                    # A syntactically valid object may still be length-cut.
                    # Keep verified findings, but never certify completeness
                    # just because every required key happened to arrive.
                    validation = StageValidation(False,
                        {**validation.trusted_output, "_verification_state": "completed_partial"},
                        "completed_partial",
                        tuple(dict.fromkeys((*validation.issues, "MODEL_OUTPUT_BUDGET_EXHAUSTED"))),
                        {**validation.record, "output_incomplete": {
                            "code": "MODEL_OUTPUT_BUDGET_EXHAUSTED", "state": "pending"}})
                error_code = ("" if validation.state in {"completed_verified", "completed_partial"}
                              else "STAGE_CANDIDATE_REJECTED")
            except StageExecutionCancelled:
                raise
            except Exception:
                # Production already has a durable checkpoint. A verifier
                # error must not erase it, repeat a model request, or prevent
                # unrelated stages from completing. Revalidation is local.
                validation = StageValidation(False, state="needs_action", issues=("STAGE_VERIFIER_FAILED",))
                error_code = "STAGE_VERIFIER_FAILED"
            self._check_cancelled()
            return StageResult(spec.stage_id, spec.identity, stage_input, frozen_candidate,
                               validation, candidate_hash, error_code,
                               execution_fingerprint=fingerprint, has_candidate=True)

        def verify(spec: StageSpec, stage_input: str, candidate: dict[str, Any],
                   fingerprint: str) -> StageResult:
            if self.candidate_verifier is None:
                return verify_local(spec, stage_input, candidate, fingerprint)
            try:
                result = self.candidate_verifier(spec, stage_input, deepcopy(candidate), fingerprint,
                                                 lambda value: verify_local(spec, stage_input, value, fingerprint))
            except StageExecutionCancelled:
                raise
            except Exception:
                return StageResult(spec.stage_id, spec.identity, stage_input, deepcopy(candidate),
                    StageValidation(False, state="needs_action", issues=("STAGE_DURABLE_VERIFIER_FAILED",)),
                    digest(candidate), "STAGE_DURABLE_VERIFIER_FAILED", execution_fingerprint=fingerprint,
                    has_candidate=True)
            if (not isinstance(result, StageResult) or result.stage_identity != spec.identity
                    or result.input_digest != stage_input or result.stage_id != spec.stage_id
                    or result.execution_fingerprint != fingerprint
                    or result.candidate_digest != digest(candidate)
                    or digest(result.candidate) != digest(candidate)):
                return StageResult(spec.stage_id, spec.identity, stage_input, deepcopy(candidate),
                    StageValidation(False, state="needs_action", issues=("STAGE_DURABLE_VERIFIER_CONTRACT_INVALID",)),
                    digest(candidate), "STAGE_DURABLE_VERIFIER_CONTRACT_INVALID", execution_fingerprint=fingerprint,
                    has_candidate=True)
            return result

        def produce(spec: StageSpec, stage_input: str, dependencies: dict[str, Any]) -> StageResult:
            self._check_cancelled()
            self.progress(spec.stage_id, "running")
            candidate = self.handlers[spec.handler_id](spec,
                deepcopy(scoped_inputs.get(spec.stage_id, frozen_inputs)), deepcopy(dependencies))
            if not isinstance(candidate, dict):
                raise ValueError("STAGE_CANDIDATE_CONTRACT_INVALID")
            self._check_cancelled()
            frozen_candidate = plain_json_value(candidate)
            try:
                # A compatibility producer may still return a transport dict
                # subclass. Persistence can retain its host receipt, while
                # validators/dependencies consume plain business JSON only.
                self.candidate_checkpoint(spec, stage_input, deepcopy(candidate))
            except StageExecutionCancelled:
                raise
            except Exception:
                # Local persistence failure is not a failed/unknown producer.
                # Try the terminal checkpoint with the received body intact;
                # if storage is entirely unavailable its error still escapes.
                return StageResult(spec.stage_id, spec.identity, stage_input, frozen_candidate,
                    StageValidation(False, state="needs_action",
                                    issues=("STAGE_CANDIDATE_CHECKPOINT_FAILED",)),
                    digest(frozen_candidate), "STAGE_CANDIDATE_CHECKPOINT_FAILED",
                    execution_fingerprint=fingerprints.get(spec.stage_id, execution_fingerprint),
                    has_candidate=True, diagnostics={"candidate_available": True,
                                                     "processing_phase": "candidate_checkpoint"})
            if self.candidate_preparer is not None:
                try:
                    prepared = deepcopy(frozen_candidate)
                    self.candidate_preparer(spec, prepared, deepcopy(scoped_inputs.get(spec.stage_id, frozen_inputs)))
                    if digest(prepared) != digest(frozen_candidate):
                        raise ValueError("STAGE_CANDIDATE_PREPARER_MUTATED_CONTENT")
                except StageExecutionCancelled:
                    raise
                except Exception:
                    # The received body is already durable. A local semantic
                    # preparation failure cannot supersede it with an empty
                    # terminal checkpoint and trigger another paid generation.
                    return StageResult(spec.stage_id, spec.identity, stage_input, frozen_candidate,
                        StageValidation(False, state="needs_action", issues=("STAGE_CANDIDATE_PREPARATION_FAILED",)),
                        digest(frozen_candidate), "STAGE_CANDIDATE_PREPARATION_FAILED",
                        execution_fingerprint=fingerprints.get(spec.stage_id, execution_fingerprint),
                        has_candidate=True)
            return verify(spec, stage_input, frozen_candidate,
                          fingerprints.get(spec.stage_id, execution_fingerprint))

        try:
            while remaining or pending:
                self._check_cancelled()
                for spec in registry.stages:
                    if spec.stage_id not in remaining or any(key not in results for key in spec.depends_on):
                        continue
                    bad_dependencies = [key for key in spec.depends_on if not results[key].usable
                                        and key not in spec.optional_dependencies]
                    capabilities = set(scoped_inputs.get(spec.stage_id, frozen_inputs).get("capabilities", ()))
                    missing_capabilities = list(spec.missing_capabilities(capabilities))
                    stage_input = expected(spec)
                    if bad_dependencies or missing_capabilities:
                        result = StageResult(spec.stage_id, spec.identity, stage_input, {},
                                             StageValidation(False, state="needs_action", issues=tuple(
                                                 [*bad_dependencies, *missing_capabilities])),
                                             digest({}), "STAGE_INPUT_CAPABILITY_UNAVAILABLE" if missing_capabilities
                                                 else "STAGE_DEPENDENCY_UNAVAILABLE",
                                             execution_fingerprint=fingerprints.get(spec.stage_id, execution_fingerprint))
                        self.checkpoint(result)
                        results[spec.stage_id] = result
                        remaining.remove(spec.stage_id)
                        self.progress(spec.stage_id, "needs_action")
                        continue
                    previous_result = saved.get(spec.stage_id)
                    if (spec.stage_id not in invalid and previous_result is not None
                            and (previous_result.usable or previous_result.has_candidate)
                            and previous_result.stage_identity == spec.identity
                            and previous_result.execution_fingerprint == fingerprints.get(spec.stage_id, execution_fingerprint)
                            and previous_result.input_digest == stage_input
                            and previous_result.candidate_digest == digest(previous_result.candidate)):
                        # Revalidate at the same seam; a persisted success bit
                        # alone is not proof of current semantic compatibility.
                        # A durable candidate is verified, not regenerated.
                        # Rejection is deterministic for this exact input;
                        # only explicit invalidation may start a new attempt.
                        reused = verify(spec, stage_input, previous_result.candidate,
                                        previous_result.execution_fingerprint)
                        self.checkpoint(reused)
                        results[spec.stage_id] = reused
                        remaining.remove(spec.stage_id)
                        self.progress(spec.stage_id, "reused_verified" if reused.validation.passed else reused.validation.state)
                        continue
                    local_candidate = local_candidates.get(spec.stage_id)
                    if (local_candidate is not None and local_candidate.has_candidate
                            and (local_only or spec.stage_id not in invalid
                                 and local_candidate.stage_identity == spec.identity
                                 and local_candidate.input_digest == stage_input
                                 and local_candidate.execution_fingerprint == fingerprints.get(
                                     spec.stage_id, execution_fingerprint))):
                        if (local_candidate.stage_id != spec.stage_id or
                                local_candidate.candidate_digest != digest(local_candidate.candidate)):
                            raise ValueError("STAGE_REVALIDATION_CANDIDATE_INVALID")
                        # Automatic continuation must not skip reading a new
                        # source, producer prompt or changed dependency. An
                        # explicitly LOCAL operation may reproject an older
                        # body under today's validator without any producer;
                        # it is not a newly generated analysis of new material.
                        # Never import old validation/output success flags.
                        migrated = verify(spec, stage_input, local_candidate.candidate,
                                          fingerprints.get(spec.stage_id, execution_fingerprint))
                        migrated = replace(migrated, diagnostics={**migrated.diagnostics,
                            "processing_phase": "local_candidate_reprojection",
                            "source_execution_fingerprint": local_candidate.execution_fingerprint,
                            "source_candidate_digest": local_candidate.candidate_digest})
                        self.checkpoint(migrated)
                        results[spec.stage_id] = migrated
                        remaining.remove(spec.stage_id)
                        self.progress(spec.stage_id, migrated.validation.state)
                        continue
                    if local_only:
                        missing = StageResult(spec.stage_id, spec.identity, stage_input, {},
                            StageValidation(False, state="needs_action", issues=("STAGE_SAVED_CANDIDATE_UNAVAILABLE",)),
                            digest({}), "STAGE_SAVED_CANDIDATE_UNAVAILABLE", execution_fingerprint=
                            fingerprints.get(spec.stage_id, execution_fingerprint))
                        self.checkpoint(missing)
                        results[spec.stage_id] = missing
                        remaining.remove(spec.stage_id)
                        self.progress(spec.stage_id, "needs_action")
                        continue
                    if len(pending) >= max_workers:
                        continue
                    self.progress(spec.stage_id, "queued")
                    dependencies = {key: dependency_findings(results[key].validation.trusted_output) for key in spec.depends_on}
                    if spec.optional_dependencies:
                        dependencies["_dependency_gaps"] = [{"stage_id": key,
                            "state": results[key].validation.state,
                            "issue_codes": list(results[key].validation.issues)}
                            for key in spec.optional_dependencies
                            if not results[key].validation.passed]
                    future = executor.submit(produce, spec, stage_input, dependencies)
                    pending[future] = (spec, stage_input)
                    remaining.remove(spec.stage_id)
                if not pending:
                    if remaining:
                        raise RuntimeError("STAGE_GRAPH_CANNOT_PROGRESS")
                    break
                finished, _ = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
                for future in finished:
                    spec, stage_input = pending.pop(future)
                    try:
                        result = future.result()
                    except StageExecutionCancelled:
                        raise
                    except Exception as exc:
                        # Stable typed diagnostic, never exception text that
                        # might contain provider secrets or source paths.
                        supplied = getattr(exc, "code", "")
                        code = (supplied if isinstance(supplied, str)
                                and re.fullmatch(r"(?:MODEL|STAGE|RECOVERY)_[A-Z0-9_]{1,70}", supplied)
                                else f"STAGE_INTERNAL_{type(exc).__name__}")
                        diagnostic = {"response_received": getattr(exc, "response_received", False) is True}
                        counting_mode = getattr(exc, "counting_mode", "")
                        if counting_mode in {"estimated_utf8_shape", "conservative_utf8_upper_bound", "provider_error_code", "provider_tokenizer",
                                             "utf8_bytes_estimate", "observed_usage_calibrated_estimate"}:
                            diagnostic["counting_mode"] = counting_mode
                            for field_name in ("required_bytes", "available_bytes"):
                                value = getattr(exc, field_name, None)
                                if type(value) is int and value >= 0:
                                    diagnostic[field_name] = value
                        phase = getattr(exc, "processing_phase", "")
                        if phase in {"candidate_storage", "response_identity", "response_parsing"}:
                            diagnostic["processing_phase"] = phase
                        path = getattr(exc, "content_path", "")
                        if path in {"$", "$.receipt.metadata.configured_model_id", "$.receipt.metadata.connection_id"}:
                            diagnostic["content_path"] = path
                        trace_id = getattr(exc, "diagnostic_trace_id", "")
                        if isinstance(trace_id, str) and re.fullmatch(r"[a-f0-9]{32}", trace_id):
                            diagnostic["trace_id"] = trace_id
                        result = StageResult(spec.stage_id, spec.identity, stage_input, {},
                                             StageValidation(False, state="needs_action"), digest({}), code,
                                             bool(getattr(exc, "retryable", False)),
                                             execution_fingerprint=fingerprints.get(spec.stage_id, execution_fingerprint),
                                             diagnostics=diagnostic)
                    self._check_cancelled()
                    self.checkpoint(result)
                    self._check_cancelled()
                    results[spec.stage_id] = result
                    self.progress(spec.stage_id, result.validation.state)
        finally:
            # Never expose a settled graph while its producers/verifiers may
            # still access repositories. Cancellation stops admission first;
            # the background graph drains already-running handlers before its
            # caller publishes the terminal job state. This is not an RPC wait.
            executor.shutdown(wait=True, cancel_futures=True)
        return {spec.stage_id: results[spec.stage_id] for spec in registry.stages if spec.stage_id in results}
