"""I/O-free recovery contracts. Fingerprints never contain credentials."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any


CONTRACT_VERSION = "recovery-v1"
MAX_CANDIDATE_JSON_BYTES = 1_048_576
_SECRET_FIELD_NAMES = frozenset({
    "apikey", "token", "accesstoken", "refreshtoken", "sessiontoken", "clientsecret", "password",
    "authorization", "credential", "credentials", "secret", "privatekey", "signedurl",
    "cookie", "bearer",
})
_USAGE_TOKEN_COUNT_FIELDS = frozenset({
    "prompttokens", "completiontokens", "totaltokens", "inputtokens", "outputtokens",
    "cachereadinputtokens", "cachewriteinputtokens", "reasoningtokens", "answertokens",
})
_RECEIPT_TOKEN_COUNT_FIELDS = frozenset({"outputbudgettokens"})


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def plain_json_value(value: Any, *, _depth: int = 0) -> Any:
    """Detach business values without reconstructing external container types.

    Unlike dataclasses.asdict/deepcopy this never calls a dict subclass's
    constructor. It preserves every JSON field; transport attributes belong
    in their host-owned envelope/journal, not in business data. No stringifying
    arbitrary objects, silent non-finite values or numeric coercion is allowed.
    """
    if _depth > 64:
        raise ValueError("JSON_VALUE_DEPTH_LIMIT")
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON_VALUE_KEY_INVALID")
        return {str(key): plain_json_value(item, _depth=_depth + 1)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain_json_value(item, _depth=_depth + 1) for item in value]
    if value is None:
        return None
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float) and math.isfinite(value):
        return float(value)
    raise ValueError("JSON_VALUE_TYPE_INVALID")


def candidate_json(value: Any) -> str:
    """Serialize bounded journal content and reject obvious credential fields."""
    return candidate_document_json(value, max_bytes=MAX_CANDIDATE_JSON_BYTES)


def candidate_document_json(value: Any, *, max_bytes: int = 64 * 1024 * 1024) -> str:
    """Validate a detached document; large documents require chunked storage.

    The 64 MiB ceiling accommodates a 16 MiB wire candidate, its independently
    verified projection and repair provenance. This is not a larger row limit:
    journal rows still use candidate_json's one MiB bound.
    """

    def contains_secret_field(item: Any, parent: str = "", *, typed_receipt_metadata: bool = False) -> bool:
        if isinstance(item, Mapping):
            detached_receipt = getattr(item, "receipt", None)
            mirrored_receipt = item.get("_response_receipt")
            is_response_receipt = (item.get("schema") == "openthesis.model-response.v1"
                                   and item.get("response_received") is True)
            is_typed_model_response = (isinstance(detached_receipt, Mapping)
                                       and detached_receipt.get("schema") == "openthesis.model-response.v1"
                                       and detached_receipt.get("response_received") is True) or (
                isinstance(mirrored_receipt, Mapping)
                and mirrored_receipt.get("schema") == "openthesis.model-response.v1"
                and mirrored_receipt.get("response_received") is True)
            for key, nested in item.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
                if normalized in _USAGE_TOKEN_COUNT_FIELDS | _RECEIPT_TOKEN_COUNT_FIELDS or (parent == "usage" and normalized == "tokens"):
                    if (normalized in _RECEIPT_TOKEN_COUNT_FIELDS
                            and not (typed_receipt_metadata or parent == "responsemeta")):
                        return True
                    if (nested is None and (typed_receipt_metadata or parent == "responsemeta")
                            and normalized in _USAGE_TOKEN_COUNT_FIELDS | _RECEIPT_TOKEN_COUNT_FIELDS):
                        continue
                    if (isinstance(nested, bool) or not isinstance(nested, int) or nested < 0):
                        return True
                    continue
                # Usage is a protocol object, not research prose. An unknown
                # token counter here is not permission to persist credentials.
                if parent in {"usage", "metadata", "responsemeta"} and "token" in normalized:
                    return True
                # ModelResponse exposes a legacy _response_meta dictionary for
                # callers, but its authoritative typed receipt is detached on
                # the object. Permit unknown known-counter values only in that
                # verified compatibility view (never in a generic user dict).
                child_is_typed_metadata = ((is_response_receipt and normalized == "metadata")
                    or (is_typed_model_response and normalized == "responsemeta"))
                if (sensitive_candidate_field(str(key))
                        or contains_secret_field(nested, normalized,
                                                 typed_receipt_metadata=child_is_typed_metadata)):
                    return True
            return False
        if isinstance(item, (tuple, list)):
            return any(contains_secret_field(nested, parent,
                                             typed_receipt_metadata=typed_receipt_metadata)
                       for nested in item)
        return False

    if contains_secret_field(value):
        raise ValueError("RECOVERY_CANDIDATE_SECRET_FIELD")
    encoded = canonical_json(value)
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError("RECOVERY_CANDIDATE_TOO_LARGE")
    return encoded


def sensitive_candidate_field(name: str) -> bool:
    """Recognise credential *fields*, not arbitrary business substrings.

    Token economics, credential versions and cookie-market analysis are valid
    research. Explicit secret names and qualified secret fields remain barred.
    This is a storage guard, not a general purpose secret-value detector.
    """
    normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
    if normalized in _SECRET_FIELD_NAMES:
        return True
    qualified = ("apikey", "apitoken", "accesstoken", "refreshtoken", "sessiontoken",
                 "clientsecret", "privatekey", "signedurl", "authorizationheader",
                 "bearertoken", "credentialvalue", "credentialsecret", "cookieheader",
                 "password", "secretkey", "credential", "secret", "authorization", "cookie", "bearer")
    return any(normalized.endswith(marker) for marker in qualified)


def candidate_fingerprint(payload: Mapping[str, Any], artifact_ids: tuple[str, ...] | list[str],
                          identity: Mapping[str, Any]) -> str:
    """Bind the durable payload, its artifact identity, and its frozen inputs."""
    record = {"candidate": payload, "artifact_ids": list(artifact_ids), "identity": dict(identity)}
    candidate_document_json(record)
    return digest(record)


class RecoveryState(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    VALIDATING = "validating"
    COMMITTED = "committed"
    PARTIAL_COMMITTED = "partial_committed"
    WAITING_EXTERNAL = "waiting_external"
    NEEDS_ACTION = "needs_action"
    BLOCKED_INTEGRITY = "blocked_integrity"
    EXHAUSTED = "exhausted"
    CANCELLED = "cancelled"
    FAILED_INTERNAL = "failed_internal"


@dataclass(frozen=True, slots=True)
class Usage:
    calls: int = 0
    tokens: int = 0
    pages: int = 0
    seconds: float = 0.0

    def __post_init__(self) -> None:
        for value in (self.calls, self.tokens, self.pages):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("RECOVERY_USAGE_INVALID")
        if (isinstance(self.seconds, bool)
                or not isinstance(self.seconds, (int, float))
                or not math.isfinite(self.seconds) or self.seconds < 0):
            raise ValueError("RECOVERY_USAGE_INVALID")

    def plus(self, other: Usage) -> Usage:
        return Usage(self.calls + other.calls, self.tokens + other.tokens,
                     self.pages + other.pages, self.seconds + other.seconds)


@dataclass(frozen=True, slots=True)
class RecoveryLimits:
    policy_version: str = CONTRACT_VERSION
    automatic_actions: int = 8
    transient_retries: int = 2
    seconds: float = 600.0
    calls: int = 0
    tokens: int = 0
    pages: int = 0
    lease_seconds: float = 30.0

    def __post_init__(self) -> None:
        if (not isinstance(self.policy_version, str) or not self.policy_version
                or isinstance(self.automatic_actions, bool)
                or not isinstance(self.automatic_actions, int)
                or isinstance(self.transient_retries, bool)
                or not isinstance(self.transient_retries, int)):
            raise ValueError("RECOVERY_LIMITS_INVALID")
        Usage(self.calls, self.tokens, self.pages, self.seconds)
        if (self.automatic_actions < 1 or self.transient_retries < 0
                or isinstance(self.lease_seconds, bool)
                or not isinstance(self.lease_seconds, (int, float))
                or not math.isfinite(self.lease_seconds) or self.lease_seconds <= 0):
            raise ValueError("RECOVERY_LIMITS_INVALID")

    def permits(self, used: Usage, reservation: Usage) -> bool:
        total = used.plus(reservation)
        return (total.calls <= self.calls and total.tokens <= self.tokens
                and total.pages <= self.pages and total.seconds <= self.seconds)


@dataclass(frozen=True, slots=True)
class AuthorizationScope:
    providers: tuple[str, ...] = ()
    page_ids: tuple[str, ...] = ()
    configuration_fingerprints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for values in (self.providers, self.page_ids, self.configuration_fingerprints):
            if (not isinstance(values, tuple)
                    or any(not isinstance(item, str) or not item.strip() for item in values)
                    or len(values) != len(set(values))):
                raise ValueError("RECOVERY_AUTHORIZATION_SCOPE_INVALID")

    def permits(self, action: RecoveryAction) -> bool:
        usage = action.estimated_usage
        needs_explicit_scope = bool(
            action.provider_id or action.page_ids
            or action.external_submission or usage.calls or usage.tokens or usage.pages
        )
        if not needs_explicit_scope:
            return True
        return (bool(action.provider_id)
                and action.provider_id in self.providers
                and bool(action.configuration_fingerprint)
                and action.configuration_fingerprint in self.configuration_fingerprints
                and set(action.page_ids).issubset(self.page_ids)
                and (not action.external_submission or bool(action.provider_id)))


@dataclass(frozen=True, slots=True)
class Issue:
    code: str
    category: str
    scope: str
    run_id: str
    input_digest: str
    affected_ids: tuple[str, ...] = ()
    stage_id: str = ""
    severity: str = "error"
    invariant_id: str = ""
    evidence_refs: tuple[str, ...] = ()
    retryability: str = "deterministic"
    diagnostic_id: str = ""

    def __post_init__(self) -> None:
        for name in ("code", "category", "scope", "run_id", "input_digest", "stage_id",
                     "severity", "invariant_id", "retryability", "diagnostic_id"):
            if not isinstance(getattr(self, name), str):
                raise ValueError("RECOVERY_ISSUE_INVALID")
        for values in (self.affected_ids, self.evidence_refs):
            if (not isinstance(values, tuple)
                    or any(not isinstance(item, str) or not item for item in values)):
                raise ValueError("RECOVERY_ISSUE_INVALID")

    @property
    def issue_id(self) -> str:
        return "issue:" + digest(asdict(self))


@dataclass(frozen=True, slots=True)
class RecoveryAction:
    strategy_id: str
    strategy_version: str
    target_ids: tuple[str, ...]
    input_digest: str
    verifier_id: str
    configuration_fingerprint: str = ""
    adapter_id: str = ""
    preconditions: tuple[str, ...] = ()
    estimated_usage: Usage = Usage()
    provider_id: str = ""
    page_ids: tuple[str, ...] = ()
    external_submission: bool = False

    def __post_init__(self) -> None:
        for name in ("strategy_id", "strategy_version", "input_digest", "verifier_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError("RECOVERY_ACTION_INVALID")
        for name in ("configuration_fingerprint", "adapter_id", "provider_id"):
            if not isinstance(getattr(self, name), str):
                raise ValueError("RECOVERY_ACTION_INVALID")
        for values in (self.target_ids, self.preconditions, self.page_ids):
            if (not isinstance(values, tuple)
                    or any(not isinstance(item, str) or not item for item in values)):
                raise ValueError("RECOVERY_ACTION_INVALID")
        if (not isinstance(self.estimated_usage, Usage)
                or not isinstance(self.external_submission, bool)):
            raise ValueError("RECOVERY_ACTION_INVALID")

    def identity(self, run_id: str, target: str) -> str:
        return digest({"run": run_id, "target": target, "action": asdict(self)})


@dataclass(frozen=True, slots=True)
class IncidentScope:
    """One budget may cover several independently leased/verifiable targets."""
    target: str
    input_digest: str

    def __post_init__(self):
        if any(not isinstance(value, str) or not value.strip() for value in (self.target, self.input_digest)):
            raise ValueError("RECOVERY_INCIDENT_SCOPE_INVALID")


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    incident_id: str
    run_id: str
    target: str
    expected_generation: int
    input_digest: str
    issues: tuple[Issue, ...]
    actions: tuple[RecoveryAction, ...]
    preserved_artifact_ids: tuple[str, ...] = ()
    invalidated_descendants: tuple[str, ...] = ()
    limits: RecoveryLimits = RecoveryLimits()
    authorization_scope: AuthorizationScope = AuthorizationScope()
    contract_version: str = CONTRACT_VERSION
    budget_scope: IncidentScope | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.incident_id, str) or not self.incident_id
                or not isinstance(self.run_id, str) or not self.run_id
                or not isinstance(self.target, str) or not self.target
                or isinstance(self.expected_generation, bool)
                or not isinstance(self.expected_generation, int)
                or self.expected_generation < 0):
            raise ValueError("RECOVERY_PLAN_IDENTITY_INVALID")
        if not isinstance(self.limits, RecoveryLimits) or not isinstance(self.authorization_scope, AuthorizationScope):
            raise ValueError("RECOVERY_PLAN_POLICY_INVALID")
        if self.budget_scope is not None and not isinstance(self.budget_scope, IncidentScope):
            raise ValueError("RECOVERY_INCIDENT_SCOPE_INVALID")
        if (not isinstance(self.contract_version, str) or not self.contract_version
                or not isinstance(self.input_digest, str) or not self.input_digest
                or not isinstance(self.issues, tuple)
                or any(not isinstance(issue, Issue) for issue in self.issues)
                or not isinstance(self.actions, tuple)
                or any(not isinstance(action, RecoveryAction) for action in self.actions)):
            raise ValueError("RECOVERY_PLAN_CONTRACT_INVALID")
        for values in (self.preserved_artifact_ids, self.invalidated_descendants):
            if (not isinstance(values, tuple)
                    or any(not isinstance(item, str) or not item for item in values)):
                raise ValueError("RECOVERY_PLAN_CONTRACT_INVALID")
        if any(issue.run_id != self.run_id or issue.input_digest != self.input_digest for issue in self.issues):
            raise ValueError("RECOVERY_ISSUE_IDENTITY_MISMATCH")
        if any(action.input_digest != self.input_digest for action in self.actions):
            raise ValueError("RECOVERY_ACTION_IDENTITY_MISMATCH")
        identities = [action.identity(self.run_id, self.target) for action in self.actions]
        if len(identities) != len(set(identities)):
            raise ValueError("RECOVERY_ACTION_DUPLICATE")

    @property
    def incident_target(self) -> str:
        return self.budget_scope.target if self.budget_scope is not None else self.target

    @property
    def incident_input_digest(self) -> str:
        return self.budget_scope.input_digest if self.budget_scope is not None else self.input_digest

    @property
    def plan_hash(self) -> str:
        return digest(asdict(self))

    @property
    def plan_id(self) -> str:
        return "plan:" + self.plan_hash

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "plan_id": self.plan_id, "plan_hash": self.plan_hash}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RecoveryPlan:
        def object_value(raw: Any, code: str) -> Mapping[str, Any]:
            if not isinstance(raw, Mapping):
                raise ValueError(code)
            return raw

        def string_tuple(raw: Any, code: str) -> tuple[str, ...]:
            if not isinstance(raw, (tuple, list)) or any(not isinstance(item, str) or not item for item in raw):
                raise ValueError(code)
            return tuple(raw)

        def action(raw: dict[str, Any]) -> RecoveryAction:
            raw = object_value(raw, "RECOVERY_ACTION_INVALID")
            usage = object_value(raw.get("estimated_usage", {}), "RECOVERY_ACTION_INVALID")
            return RecoveryAction(**{
                **raw,
                "target_ids": string_tuple(raw.get("target_ids"), "RECOVERY_ACTION_INVALID"),
                "preconditions": string_tuple(raw.get("preconditions", ()), "RECOVERY_ACTION_INVALID"),
                "page_ids": string_tuple(raw.get("page_ids", ()), "RECOVERY_ACTION_INVALID"),
                "estimated_usage": Usage(**usage),
            })
        value = object_value(value, "RECOVERY_PLAN_INVALID")
        auth = object_value(value.get("authorization_scope", {}), "RECOVERY_AUTHORIZATION_SCOPE_INVALID")
        if set(auth) - {"providers", "page_ids", "configuration_fingerprints"}:
            raise ValueError("RECOVERY_AUTHORIZATION_SCOPE_INVALID")
        raw_limits = object_value(value.get("limits", {}), "RECOVERY_LIMITS_INVALID")
        raw_issues = value.get("issues")
        raw_actions = value.get("actions")
        if not isinstance(raw_issues, (tuple, list)) or not isinstance(raw_actions, (tuple, list)):
            raise ValueError("RECOVERY_PLAN_CONTRACT_INVALID")

        def issue(raw: dict[str, Any]) -> Issue:
            raw = object_value(raw, "RECOVERY_ISSUE_INVALID")
            return Issue(**{
                **raw,
                "affected_ids": string_tuple(raw.get("affected_ids", ()), "RECOVERY_ISSUE_INVALID"),
                "evidence_refs": string_tuple(raw.get("evidence_refs", ()), "RECOVERY_ISSUE_INVALID"),
            })

        result = cls(
            incident_id=value["incident_id"], run_id=value["run_id"], target=value["target"],
            expected_generation=value["expected_generation"], input_digest=value["input_digest"],
            issues=tuple(issue(item) for item in raw_issues),
            actions=tuple(action(item) for item in raw_actions),
            preserved_artifact_ids=string_tuple(value.get("preserved_artifact_ids", ()), "RECOVERY_PLAN_CONTRACT_INVALID"),
            invalidated_descendants=string_tuple(value.get("invalidated_descendants", ()), "RECOVERY_PLAN_CONTRACT_INVALID"),
            limits=RecoveryLimits(**raw_limits),
            authorization_scope=AuthorizationScope(**{
                key: string_tuple(auth.get(key, ()), "RECOVERY_AUTHORIZATION_SCOPE_INVALID")
                for key in ("providers", "page_ids", "configuration_fingerprints")}),
            contract_version=value.get("contract_version", CONTRACT_VERSION),
            budget_scope=(IncidentScope(**object_value(value["budget_scope"], "RECOVERY_INCIDENT_SCOPE_INVALID"))
                          if value.get("budget_scope") is not None else None),
        )
        if (value.get("plan_hash", result.plan_hash) != result.plan_hash
                or result.contract_version != CONTRACT_VERSION):
            raise ValueError("RECOVERY_PLAN_HASH_OR_VERSION_MISMATCH")
        return result


@dataclass(frozen=True, slots=True)
class VerificationResult:
    passed: bool
    resolved_issue_ids: tuple[str, ...] = ()
    remaining_issue_ids: tuple[str, ...] = ()
    invariant_failures: tuple[str, ...] = ()
    accepted_delta: tuple[str, ...] = ()
    invalidated_dependencies: tuple[str, ...] = ()
    provenance: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.passed, bool):
            raise ValueError("RECOVERY_VERIFIER_CONTRACT_INVALID")
        for values in (self.resolved_issue_ids, self.remaining_issue_ids, self.invariant_failures,
                       self.accepted_delta, self.invalidated_dependencies):
            if (not isinstance(values, tuple)
                    or any(not isinstance(item, str) or not item for item in values)
                    or len(values) != len(set(values))):
                raise ValueError("RECOVERY_VERIFIER_CONTRACT_INVALID")
        if (not isinstance(self.provenance, tuple) or any(
                not isinstance(item, tuple) or len(item) != 2
                or any(not isinstance(value, str) or not value for value in item) for item in self.provenance)):
            raise ValueError("RECOVERY_VERIFIER_CONTRACT_INVALID")


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    state: RecoveryState
    resolved_issue_ids: tuple[str, ...] = ()
    remaining_issue_ids: tuple[str, ...] = ()
    candidate_artifact_ids: tuple[str, ...] = ()
    committed_revision_id: str = ""
    next_actions: tuple[str, ...] = ()
    usage: Usage = Usage()
    diagnostic_code: str = ""
    accepted_delta: tuple[str, ...] = ()
    invalidated_dependencies: tuple[str, ...] = ()
    provenance: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
