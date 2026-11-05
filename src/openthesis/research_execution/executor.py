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
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping

from ..resilience.contracts import digest, plain_json_value
from .registry import StageRegistry, StageSpec


class StageExecutionCancelled(RuntimeError):
    pass


def dependency_findings(value: Any, _memo=None, *, legacy_order=False) -> Any:
    """The actual downstream input, excluding host proof/prompt history.

    Proof records stay in their stage checkpoint. Including their changing
    local audit hashes in producer input makes identical business findings
    look new and repeats already completed calls after local revalidation.
    """
    memo = {} if _memo is None else _memo
    if isinstance(value, (dict, list, tuple)):
        marker = id(value)
        if marker in memo:
            return memo[marker]
        result = {} if isinstance(value, dict) else []
        memo[marker] = result
        if isinstance(value, dict):
            result.update({key: dependency_findings(child, memo, legacy_order=legacy_order) for key, child in value.items()
                if (not str(key).startswith("_") or key == "_verification_state")
                and key not in {"trusted_channels", "prior_artifacts", "raw_response", "prompt", "journal"}})
            audit = value.get("_section_validation")
            if isinstance(audit, dict):
                # Stable per-leaf trust is actual producer input, unlike
                # local proof hashes/timings. Retain its meaning without
                # copying the entire changing checkpoint into each request.
                fields = {"path", "section_id", "validation_state", "role", "evidence_ids",
                          "display_eligible", "calculation_eligible", "display_state"}
                nodes = [{key: dependency_findings(child, memo, legacy_order=legacy_order)
                          for key, child in node.items() if key in fields}
                         for node in audit.get("nodes", ()) if isinstance(node, dict)]
                if not legacy_order:
                    # SQLite canonical JSON sorts object keys. That may change
                    # validator traversal order, not the proof at a given path.
                    nodes.sort(key=lambda node: (str(node.get("path", "")), digest(node)))
                result["_section_validation"] = {"schema": audit.get("schema"), "nodes": nodes}
            if type(value.get("_verified_claim_count")) is int:
                result["_verified_claim_count"] = value["_verified_claim_count"]
        else:
            result.extend(dependency_findings(child, memo, legacy_order=legacy_order) for child in value)
        return result
    return deepcopy(value)


def dependency_request_values(spec: StageSpec, dependencies: Mapping[str, "StageResult"], *, legacy_order=False) -> dict:
    values = {key: dependency_findings(dependencies[key].validation.trusted_output, legacy_order=legacy_order)
              for key in spec.depends_on if key in dependencies}
    if spec.optional_dependencies:
        values["_dependency_gaps"] = [{"stage_id": key, "state": dependencies[key].validation.state,
            "issue_codes": list(dependencies[key].validation.issues) if legacy_order
                else sorted(set(dependencies[key].validation.issues))}
            for key in spec.optional_dependencies if key in dependencies and not dependencies[key].validation.passed]
    return values


def stage_input_components(spec: StageSpec, input_identity: str, fingerprint: str,
                           dependencies: Mapping[str, "StageResult"], *, legacy=False) -> dict:
    """Separate model-visible inputs from verifier history.

    Optional dependency gaps and in-body trust flags are deliberately part of
    the input: these can be seen by the producer. Outer validation status and
    issue codes for required dependencies do not enter its request.
    """
    values = dependency_request_values(spec, dependencies, legacy_order=legacy)
    result = {"input": input_identity, "stage": spec.identity, "execution_fingerprint": fingerprint,
              "dependencies": {key: {"stage_identity": dependencies[key].stage_identity,
                                    "trusted_output_sha256": digest(values[key])}
                               for key in spec.depends_on}}
    if legacy:
        for key, record in result["dependencies"].items():
            record.update(validation_state=dependencies[key].validation.state,
                          issue_codes=dependencies[key].validation.issues)
    elif values.get("_dependency_gaps"):
        result["visible_dependency_gaps_sha256"] = digest(values["_dependency_gaps"])
    return result


def stage_input_digest(spec: StageSpec, input_identity: str, fingerprint: str,
                       dependencies: Mapping[str, "StageResult"]) -> str:
    return digest(stage_input_components(spec, input_identity, fingerprint, dependencies))


def stage_generation_matches(spec: StageSpec, input_identity: str, fingerprint: str,
                             saved: Mapping[str, "StageResult"], current: Mapping[str, "StageResult"],
                             previous_digest: str, expected: str | None = None, *,
                             legacy_material_identity: str | None = None) -> bool:
    if expected is None:
        expected = stage_input_digest(spec, input_identity, fingerprint, current)
    if previous_digest == expected:
        return True
    # A pre-3.3.3 checkpoint is usable only when the exact old digest is
    # reproducible and every current model-visible dependency still matches.
    if not all(key in saved for key in spec.depends_on):
        return False
    return (previous_digest == digest(stage_input_components(spec,
                legacy_material_identity or input_identity, fingerprint, saved, legacy=True))
            and expected == stage_input_digest(spec, input_identity, fingerprint, saved))


def stage_material_identities(registry: StageRegistry, inputs: dict, scoped_inputs: Mapping[str, dict], *,
                              legacy=False) -> dict[str, str]:
    identities = {}
    result = {}
    for spec in registry.stages:
        value = scoped_inputs.get(spec.stage_id, inputs)
        key = id(value)
        if key not in identities:
            # These host inputs select rendering and verification policy,
            # not generation. The output/report language remains visible.
            if legacy and callable(getattr(value.get("context_json"), "legacy_text", None)):
                legacy_value = {**value, "context_json": value["context_json"].legacy_text()}
            else:
                legacy_value = value
            identities[key] = digest(legacy_value if legacy else {k: v for k, v in value.items()
                if k not in {"research_verification_contract", "ui_language"}})
        result[spec.stage_id] = identities[key]
    return result


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
        candidate_wave_preparer: Callable[..., None] | None = None,
        dispatch_group: Callable[[StageSpec], str] | None = None,
        timing_observer: Callable[[str, dict], None] | None = None,
        candidate_verifier: Callable[..., StageResult] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        progress: Callable[[str, str], None] | None = None,
    ):
        self.handlers = MappingProxyType(dict(handlers))
        self.validators = MappingProxyType(dict(validators))
        self.checkpoint = checkpoint or (lambda _result: None)
        self.candidate_checkpoint = candidate_checkpoint or (lambda _spec, _digest, _candidate: None)
        self.candidate_preparer = candidate_preparer
        if candidate_preparer is not None and candidate_wave_preparer is not None:
            raise ValueError("STAGE_PREPARER_MUST_NOT_DOUBLE_SUBMIT")
        self.candidate_wave_preparer = candidate_wave_preparer
        self.dispatch_group = dispatch_group
        self.timing_observer = timing_observer
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
        input_identities = stage_material_identities(registry, frozen_inputs, scoped_inputs)
        selected = set(registry.ancestors(targets)) if targets is not None else {spec.stage_id for spec in registry.stages}
        invalid = set(registry.descendants(invalidated)) if propagate_invalidation else set(invalidated)
        for stage_id in invalid:
            registry.get(stage_id)
        remaining = set(selected)
        wave_candidates = {}
        results: dict[str, StageResult] = {}
        saved = dict(previous or {})
        legacy_inputs = stage_material_identities(registry, frozen_inputs, scoped_inputs, legacy=True) if saved else {}
        local_candidates = dict(revalidation_candidates or {})
        if execution_fingerprints is None and invalidated:
            for spec in registry.stages:
                prior = saved.get(spec.stage_id)
                if (spec.stage_id not in invalid and prior is not None
                        and prior.execution_fingerprint and prior.stage_identity == spec.identity
                        and all(key in saved for key in spec.depends_on)
                        and stage_generation_matches(spec, input_identities[spec.stage_id],
                            prior.execution_fingerprint, saved, saved, prior.input_digest,
                            legacy_material_identity=legacy_inputs.get(spec.stage_id))):
                    fingerprints[spec.stage_id] = prior.execution_fingerprint
        executor = ThreadPoolExecutor(max_workers=max_workers)
        pending = {}
        timeline = {}
        cold_leads = {}
        attempted_packets = set()

        def checkpoint(result):
            details = timeline.get(result.stage_id, {})
            # Timing must not change a content-addressed trusted checkpoint.
            # The same immutable object is already committed by the verifier.
            started = time.monotonic()
            self.checkpoint(result)
            if self.timing_observer is not None:
                safe = {key: value for key, value in details.items() if not key.startswith("_")}
                safe.update(publication_seconds=max(0, time.monotonic() - started), published_at_epoch=time.time())
                try:
                    self.timing_observer(result.stage_id, safe)
                except Exception:
                    # Optional attribution cannot invalidate saved research or
                    # turn a received response into another remote submission.
                    pass

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

        def verify_candidate(spec: StageSpec, stage_input: str, candidate: dict[str, Any],
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

        def verify(spec, stage_input, candidate, fingerprint):
            started = time.monotonic()
            details = timeline.setdefault(spec.stage_id, {"depends_on": list(spec.depends_on)})
            details["verification_started_at_epoch"] = time.time()
            try:
                return verify_candidate(spec, stage_input, candidate, fingerprint)
            finally:
                details["local_verification_seconds"] = max(0, time.monotonic() - started)
                details["verification_finished_at_epoch"] = time.time()

        def produce(spec: StageSpec, stage_input: str, dependencies: dict[str, Any]) -> StageResult:
            self._check_cancelled()
            self.progress(spec.stage_id, "running")
            details = timeline[spec.stage_id]
            started = time.monotonic()
            details.update(producer_started_at_epoch=time.time(), queue_seconds=max(0, started - details["_queued"]))
            try:
                candidate = self.handlers[spec.handler_id](spec,
                    deepcopy(scoped_inputs.get(spec.stage_id, frozen_inputs)), deepcopy(dependencies))
            finally:
                details.update(producer_seconds=max(0, time.monotonic() - started), producer_finished_at_epoch=time.time())
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
            if self.candidate_wave_preparer is not None:
                # Free this worker before collecting the ready wave. Waiting
                # at a worker-side barrier deadlocks when wave > pool size.
                return StageResult(spec.stage_id, spec.identity, stage_input, frozen_candidate,
                    StageValidation(False, state="needs_action"), digest(frozen_candidate),
                    execution_fingerprint=fingerprints.get(spec.stage_id, execution_fingerprint),
                    has_candidate=True, diagnostics={"wave_preparation_pending": True})
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
            while remaining or pending or wave_candidates:
                self._check_cancelled()
                for spec in registry.stages:
                    if spec.stage_id not in remaining or any(key not in results for key in spec.depends_on):
                        continue
                    bad_dependencies = [key for key in spec.depends_on if not results[key].usable
                                        and key not in spec.optional_dependencies]
                    capabilities = set(scoped_inputs.get(spec.stage_id, frozen_inputs).get("capabilities", ()))
                    missing_capabilities = list(spec.missing_capabilities(capabilities))
                    stage_input = expected(spec)
                    timeline.setdefault(spec.stage_id, {"depends_on": list(spec.depends_on),
                        "ready_at_epoch": time.time()})
                    if bad_dependencies or missing_capabilities:
                        result = StageResult(spec.stage_id, spec.identity, stage_input, {},
                                             StageValidation(False, state="needs_action", issues=tuple(
                                                 [*bad_dependencies, *missing_capabilities])),
                                             digest({}), "STAGE_INPUT_CAPABILITY_UNAVAILABLE" if missing_capabilities
                                                 else "STAGE_DEPENDENCY_UNAVAILABLE",
                                             execution_fingerprint=fingerprints.get(spec.stage_id, execution_fingerprint))
                        checkpoint(result)
                        results[spec.stage_id] = result
                        remaining.remove(spec.stage_id)
                        self.progress(spec.stage_id, "needs_action")
                        continue
                    previous_result = saved.get(spec.stage_id)
                    if (spec.stage_id not in invalid and previous_result is not None
                            and (previous_result.usable or previous_result.has_candidate)
                            and previous_result.stage_identity == spec.identity
                            and previous_result.execution_fingerprint == fingerprints.get(spec.stage_id, execution_fingerprint)
                            and stage_generation_matches(spec, input_identities[spec.stage_id],
                                previous_result.execution_fingerprint, saved, results, previous_result.input_digest,
                                stage_input, legacy_material_identity=legacy_inputs.get(spec.stage_id))
                            and previous_result.candidate_digest == digest(previous_result.candidate)):
                        # Revalidate at the same seam; a persisted success bit
                        # alone is not proof of current semantic compatibility.
                        # A durable candidate is verified, not regenerated.
                        # Rejection is deterministic for this exact input;
                        # only explicit invalidation may start a new attempt.
                        reused = verify(spec, stage_input, previous_result.candidate,
                                        previous_result.execution_fingerprint)
                        checkpoint(reused)
                        results[spec.stage_id] = reused
                        remaining.remove(spec.stage_id)
                        self.progress(spec.stage_id, "reused_verified" if reused.validation.passed else reused.validation.state)
                        continue
                    local_candidate = local_candidates.get(spec.stage_id)
                    if (local_candidate is not None and local_candidate.has_candidate
                            and (local_only or spec.stage_id not in invalid
                                 and local_candidate.stage_identity == spec.identity
                                 and stage_generation_matches(spec, input_identities[spec.stage_id],
                                     local_candidate.execution_fingerprint, saved, results, local_candidate.input_digest,
                                     stage_input, legacy_material_identity=legacy_inputs.get(spec.stage_id))
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
                        checkpoint(migrated)
                        results[spec.stage_id] = migrated
                        remaining.remove(spec.stage_id)
                        self.progress(spec.stage_id, migrated.validation.state)
                        continue
                    if local_only:
                        missing = StageResult(spec.stage_id, spec.identity, stage_input, {},
                            StageValidation(False, state="needs_action", issues=("STAGE_SAVED_CANDIDATE_UNAVAILABLE",)),
                            digest({}), "STAGE_SAVED_CANDIDATE_UNAVAILABLE", execution_fingerprint=
                            fingerprints.get(spec.stage_id, execution_fingerprint))
                        checkpoint(missing)
                        results[spec.stage_id] = missing
                        remaining.remove(spec.stage_id)
                        self.progress(spec.stage_id, "needs_action")
                        continue
                    if len(pending) >= max_workers:
                        continue
                    packet = self.dispatch_group(spec) if self.dispatch_group is not None else ""
                    # A necessary first task may establish the same packet's
                    # remote prefix. Other packets still use the free slot;
                    # there is no warmup call, retry probe or global barrier.
                    if packet and packet not in attempted_packets and packet in cold_leads:
                        continue
                    self.progress(spec.stage_id, "queued")
                    timeline[spec.stage_id].update(queued_at_epoch=time.time(), _queued=time.monotonic(),
                                                   dispatch_group=packet, executor_workers=max_workers)
                    if previous_result is not None and all(key in saved for key in spec.depends_on):
                        old = stage_input_components(spec, input_identities[spec.stage_id],
                            previous_result.execution_fingerprint, saved)
                        new = stage_input_components(spec, input_identities[spec.stage_id],
                            fingerprints.get(spec.stage_id, execution_fingerprint), results)
                        changed = [key for key in spec.depends_on if old["dependencies"][key] != new["dependencies"][key]]
                        before = dependency_request_values(spec, saved)
                        after = dependency_request_values(spec, results)
                        timeline[spec.stage_id]["generation_change"] = {
                            "previous_identity_matches_current_material": digest(old) == previous_result.input_digest,
                            "changed_dependencies": changed,
                            # Only host-known structural fields, not generated
                            # field names/values or source bodies, are exported.
                            "changed_dependency_categories": {key: {
                                "proof": before[key].get("_section_validation") != after[key].get("_section_validation"),
                                "verification_state": before[key].get("_verification_state") != after[key].get("_verification_state"),
                                "claims": before[key].get("claims") != after[key].get("claims"),
                                "body": {k: v for k, v in before[key].items() if not k.startswith("_") and k != "claims"}
                                    != {k: v for k, v in after[key].items() if not k.startswith("_") and k != "claims"},
                            } for key in changed},
                            "visible_gaps_changed": old.get("visible_dependency_gaps_sha256") != new.get("visible_dependency_gaps_sha256"),
                            "model_changed": old["execution_fingerprint"] != new["execution_fingerprint"],
                            "explicitly_invalidated": spec.stage_id in invalid,
                        }
                    dependencies = dependency_request_values(spec, results)
                    future = executor.submit(produce, spec, stage_input, dependencies)
                    pending[future] = (spec, stage_input)
                    if packet and packet not in attempted_packets:
                        cold_leads[packet] = future
                    remaining.remove(spec.stage_id)
                if not pending:
                    if wave_candidates:
                        batch = [wave_candidates[key] for key in sorted(wave_candidates)]
                        wave_candidates.clear()
                        supplied = [(spec, deepcopy(result.candidate), deepcopy(scoped_inputs.get(spec.stage_id, frozen_inputs)))
                                    for spec, _stage_input, result in batch]
                        prepared = True
                        wave_started = time.monotonic()
                        wave_id = digest([spec.stage_id for spec, _stage, _candidate in batch])
                        try:
                            self.candidate_wave_preparer(supplied)
                            if any(digest(candidate) != result.candidate_digest
                                   for (_spec, candidate, _inputs), (_original, _identity, result) in zip(supplied, batch)):
                                raise ValueError("STAGE_CANDIDATE_PREPARER_MUTATED_CONTENT")
                        except StageExecutionCancelled:
                            raise
                        except Exception:
                            prepared = False
                        for spec, stage_input, candidate in batch:
                            timeline.setdefault(spec.stage_id, {}).update(shared_wave_id=wave_id,
                                shared_wave_verification_seconds=max(0, time.monotonic() - wave_started))
                            self._check_cancelled()
                            result = (verify(spec, stage_input, candidate.candidate,
                                fingerprints.get(spec.stage_id, execution_fingerprint)) if prepared else replace(candidate,
                                    error_code="STAGE_WAVE_PREPARATION_FAILED",
                                    validation=StageValidation(False, state="needs_action", issues=("STAGE_WAVE_PREPARATION_FAILED",)),
                                    diagnostics={"candidate_available": True, "processing_phase": "wave_preparation"}))
                            checkpoint(result)
                            results[spec.stage_id] = result
                            self.progress(spec.stage_id, result.validation.state)
                        continue
                    if remaining:
                        raise RuntimeError("STAGE_GRAPH_CANNOT_PROGRESS")
                    break
                finished, _ = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
                for future in finished:
                    spec, stage_input = pending.pop(future)
                    for packet, lead in tuple(cold_leads.items()):
                        if lead is future:
                            attempted_packets.add(packet)
                            del cold_leads[packet]
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
                        if phase in {"candidate_storage", "response_identity", "response_parsing", "request_compilation"}:
                            diagnostic["processing_phase"] = phase
                        if phase == "request_compilation" and code == "MODEL_REQUEST_COMPILATION_FAILED":
                            from .request_diagnostics import safe_compilation_diagnostic
                            compilation = safe_compilation_diagnostic(getattr(exc, "compilation_diagnostic", None))
                            if compilation is not None:
                                diagnostic["request_compilation"] = {**compilation, "stage_id": spec.stage_id}
                                diagnostic["not_sent"] = True
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
                    if result.diagnostics.get("wave_preparation_pending") and result.has_candidate:
                        wave_candidates[spec.stage_id] = (spec, stage_input, result)
                        continue
                    self._check_cancelled()
                    checkpoint(result)
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
