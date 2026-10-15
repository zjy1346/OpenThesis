"""A field/path allowlist for claim repair; business trust stays with verifier.

No I/O, formula guessing or provider call lives here. The model executor and
durable recovery coordinator own the one additional request and shared budget.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import json
from typing import Any, Callable

from .resilience.contracts import digest
from .research_execution.model_output import (
    TargetRepairResult,
    repaired_output_valid,
    response_refused,
)

CLAIM_REPAIR_VERSION = "claim-path-repair-v1"


def stable_claim_id(claim: Any, path: str) -> str:
    explicit = claim.get("claim_id") if isinstance(claim, dict) else None
    if isinstance(explicit, str) and explicit.strip():
        return explicit
    try:
        return "claim:" + digest({"path": path, "claim": claim})[:24]
    except (TypeError, ValueError):
        # Invalid JSON cannot be repaired or published. Still give its audit
        # entry a stable path identity instead of crashing the verifier.
        return "claim:invalid:" + digest({"path": path})[:24]


@dataclass(frozen=True, slots=True)
class ClaimValidation:
    claim_id: str
    path: str
    state: str
    issue_code: str
    missing_fields: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    normalized_claim: dict[str, Any]
    repairability: str

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ClaimRepairTarget:
    path: str
    claim_id: str
    fields: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    issue_code: str


@dataclass(frozen=True, slots=True)
class ClaimRepairPlan:
    candidate_digest: str
    validator_version: str
    targets: tuple[ClaimRepairTarget, ...]
    version: str = CLAIM_REPAIR_VERSION

    def to_dict(self):
        return asdict(self)


def claim_arrays(value, path=""):
    """Yield explicit domain claim arrays with stable escaped JSON pointers."""
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).startswith("_"):
                continue
            pointer = path + "/" + str(key).replace("~", "~0").replace("/", "~1")
            if key == "claims" and isinstance(child, list):
                yield pointer, child
            else:
                yield from claim_arrays(child, pointer)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from claim_arrays(child, path + "/" + str(index))


def read_pointer(value, path):
    for token in path.split("/")[1:]:
        key = token.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def _truncated(value):
    metadata = value.get("_response_meta")
    return isinstance(metadata, dict) and metadata.get("finish_reason") in {
        "length", "max_tokens", "max_output_tokens"}


class ClaimRepairContract:
    def __init__(self, verify: Callable[[dict], dict], validator_version: str, *,
                 repair_material: Callable[[tuple[ClaimRepairTarget, ...]], dict] | None = None):
        self.verify, self.validator_version = verify, validator_version
        self.repair_material = repair_material

    def plan(self, candidate: dict) -> ClaimRepairPlan | None:
        if (not isinstance(candidate, dict) or response_refused(candidate)
                or candidate.get("structured_output_valid") is not True
                or candidate.get("_response_error") not in (None, "") or _truncated(candidate)):
            return None
        try:
            candidate_digest = digest(candidate)
        except (TypeError, ValueError):
            return None
        targets = []
        for path, claims in claim_arrays(candidate):
            check = self.verify({"claims": claims})
            for item in check.get("claim_verifications", ()):
                index = item["index"]
                claim = claims[index]
                if not isinstance(claim, dict) or item.get("state") in {"numeric_verified", "text_supported", "reference_only"}:
                    continue
                if item.get("repairability") != "field_patch":
                    continue
                fields = tuple(item.get("missing_fields", ()))
                if not fields:
                    continue
                claim_path = path + "/" + str(index)
                claim_id = stable_claim_id(claim, claim_path)
                targets.append(ClaimRepairTarget(claim_path, claim_id, fields,
                    tuple(claim.get("evidence_ids", ())), item["issue_code"]))
        return ClaimRepairPlan(candidate_digest, self.validator_version, tuple(targets)) if targets else None

    def prompt(self, user_prompt: str, candidate: dict, plan: ClaimRepairPlan) -> str:
        if digest(candidate) != plan.candidate_digest:
            raise ValueError("CLAIM_REPAIR_CANDIDATE_STALE")
        entries = [{**asdict(target), "original_claim": read_pointer(candidate, target.path)} for target in plan.targets]
        return user_prompt + "\n\n[OpenThesis claim-path-repair-v1]\n" + json.dumps({
            "candidate_digest": plan.candidate_digest, "validator_version": plan.validator_version,
            "targets": entries,
            "repair_material": self.repair_material(plan.targets) if self.repair_material else {},
            "response": {"claim_patches": [{"path": "exact target path", "claim_id": "exact stable ID", "fields": {"allowed field only": "value"}}]},
            "rules": "Return only claim_patches for every listed target. Preserve claim kind, meaning and evidence IDs. Use only cited canonical evidence and explicit supported formulas with input evidence IDs. Do not modify healthy sections, remove claims, add sources, guess a formula or turn numeric claims into opinion. If evidence cannot support a repair, do not invent a patch.",
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def merge_targets(self, original: dict, repair: dict,
                      plan: ClaimRepairPlan) -> TargetRepairResult:
        """Apply each frozen claim target atomically and independently.

        A malformed or unverifiable target cannot poison a valid sibling.  A
        duplicate target is intentionally quarantined rather than resolved by
        list order.  The returned candidate is still untrusted output.
        """
        baseline = deepcopy(original) if isinstance(original, dict) else {}
        target_paths = tuple(target.path for target in plan.targets)
        issues: list[dict[str, str]] = []
        unresolved: list[str] = []
        accepted: list[str] = []
        ignored: list[str] = []

        try:
            original_digest = digest(original)
            current_plan = self.plan(original)
        except (TypeError, ValueError):
            original_digest, current_plan = "", None
        if (not isinstance(original, dict) or original_digest != plan.candidate_digest
                or self.validator_version != plan.validator_version or current_plan != plan):
            unresolved.extend(target_paths)
            issues.append({"path": "/", "code": "CLAIM_REPAIR_PLAN_STALE"})
            return TargetRepairResult(baseline, (), tuple(unresolved), (), tuple(issues), False)

        if not isinstance(repair, dict) or not repaired_output_valid(repair) or _truncated(repair):
            unresolved.extend(target_paths)
            issues.append({"path": "/", "code": "CLAIM_REPAIR_RESPONSE_INVALID"})
            return TargetRepairResult(baseline, (), tuple(unresolved), (), tuple(issues), False)

        control = {"structured_output_valid", "_response_meta", "_response_error", "raw_response",
                   "claim_patches"}
        ignored.extend(sorted(str(key) for key in repair if key not in control))
        patches = repair.get("claim_patches")
        if not isinstance(patches, list):
            unresolved.extend(target_paths)
            issues.append({"path": "claim_patches", "code": "CLAIM_REPAIR_TARGETS_MISSING"})
            return TargetRepairResult(baseline, (), tuple(unresolved), tuple(ignored), tuple(issues), False)

        by_path = {target.path: target for target in plan.targets}
        grouped: dict[str, list[Any]] = {path: [] for path in target_paths}
        for patch in patches:
            path = patch.get("path") if isinstance(patch, dict) else None
            if isinstance(path, str) and path in grouped:
                grouped[path].append(patch)
            else:
                ignored.append(f"claim_patches.{path if isinstance(path, str) else 'invalid'}")

        merged = deepcopy(baseline)
        for target in plan.targets:
            candidates = grouped[target.path]
            if len(candidates) != 1:
                unresolved.append(target.path)
                issues.append({
                    "path": target.path,
                    "code": "CLAIM_REPAIR_TARGET_DUPLICATE" if len(candidates) > 1
                    else "CLAIM_REPAIR_TARGET_MISSING",
                })
                continue
            patch = candidates[0]
            fields = patch.get("fields") if isinstance(patch, dict) else None
            if (not isinstance(patch, dict) or set(patch) != {"path", "claim_id", "fields"}
                    or patch.get("claim_id") != target.claim_id
                    or not isinstance(fields, dict) or set(fields) != set(target.fields)):
                unresolved.append(target.path)
                issues.append({"path": target.path, "code": "CLAIM_REPAIR_TARGET_INVALID"})
                continue
            try:
                claim = read_pointer(merged, target.path)
            except (KeyError, IndexError, TypeError, ValueError):
                claim = None
            if not isinstance(claim, dict):
                unresolved.append(target.path)
                issues.append({"path": target.path, "code": "CLAIM_REPAIR_TARGET_INVALID"})
                continue
            proposed = deepcopy(claim)
            proposed.update(deepcopy(fields))
            if tuple(proposed.get("evidence_ids", ())) != target.evidence_ids:
                unresolved.append(target.path)
                issues.append({"path": target.path, "code": "CLAIM_REPAIR_EVIDENCE_ID_CHANGED"})
                continue
            check: dict[str, Any] = {}
            try:
                verification = self.verify({"claims": [proposed]})
                checks = verification.get("claim_verifications", ()) if isinstance(verification, dict) else ()
                check = checks[0] if checks else {}
                state = check.get("state") if isinstance(check, dict) else None
            except (TypeError, ValueError, KeyError, IndexError):
                state = None
            if state not in {"numeric_verified", "text_supported"}:
                unresolved.append(target.path)
                issues.append({
                    "path": target.path,
                    "code": (check.get("issue_code") if isinstance(check, dict)
                             and isinstance(check.get("issue_code"), str)
                             and check.get("issue_code") else "CLAIM_REPAIR_TARGET_UNVERIFIED"),
                })
                continue
            merged_claim = deepcopy(proposed)
            parent = merged
            tokens = target.path.split("/")[1:]
            for token in tokens[:-1]:
                token = token.replace("~1", "/").replace("~0", "~")
                parent = parent[int(token)] if isinstance(parent, list) else parent[token]
            leaf = tokens[-1].replace("~1", "/").replace("~0", "~")
            if isinstance(parent, list):
                parent[int(leaf)] = merged_claim
            else:
                parent[leaf] = merged_claim
            accepted.append(target.path)
        return TargetRepairResult(
            merged, tuple(accepted), tuple(unresolved), tuple(sorted(set(ignored))),
            tuple(issues), bool(accepted),
        )

    def merge(self, original: dict, repair: dict, plan: ClaimRepairPlan) -> dict | None:
        """Compatibility wrapper: return a candidate only when any target progressed."""
        result = self.merge_targets(original, repair, plan)
        return result.candidate if result.progress else None
