from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from .domain import ResearchArtifact, ResearchRun
from .i18n import EN, ZH_HANT, normalize_language
from .storage import Storage
from .storage_comparison import ComparisonError
from .research_continuity import StageAttempt, StageOutcome


def _final_report(content: dict[str, Any]) -> dict[str, Any]:
    report = content.get("report", content)
    if not isinstance(report, dict) or not report:
        raise ComparisonError("COMPARISON_REPORT_UNAVAILABLE")
    return report


def _claim_texts(report: dict[str, Any]) -> set[str]:
    claims = report.get("claims", [])
    if not isinstance(claims, list):
        return set()
    results = set()
    for claim in claims:
        if isinstance(claim, dict) and claim.get("text"):
            results.add(str(claim["text"]).strip())
        elif isinstance(claim, str):
            results.add(claim.strip())
    return {item for item in results if item}


def compare_research_runs(
    storage: Storage,
    primary: ResearchRun,
    secondary: ResearchRun,
    language: str = "zh-CN",
    *,
    cancel_check: Callable[[], bool] | None = None,
    stage_id: str = "model-comparison",
) -> ResearchArtifact:
    locale = normalize_language(language)
    english = locale == EN
    snapshot = storage.get_comparison_snapshot(primary.run_id, secondary.run_id)
    left = _final_report(snapshot["primary"]["report_payload"])
    right = _final_report(snapshot["secondary"]["report_payload"])
    left_claims = _claim_texts(left)
    right_claims = _claim_texts(right)
    ignored = {"claims", "structured_output_valid"}
    sections = sorted((set(left) | set(right)) - ignored)
    section_comparison = []
    for section in sections:
        left_value = left.get(section)
        right_value = right.get(section)
        left_json = json.dumps(left_value, ensure_ascii=False, sort_keys=True)
        right_json = json.dumps(right_value, ensure_ascii=False, sort_keys=True)
        section_comparison.append(
            {
                "section": section,
                "same": left_json == right_json,
                "primary": left_value,
                "secondary": right_value,
            }
        )
    content = {
        role: {key: snapshot[role][key] for key in
               ("run_id", "provider", "model", "revision_id", "generation", "content_sha256")}
        for role in ("primary", "secondary")
    }
    content.update({
        "common_claims": sorted(left_claims & right_claims),
        "primary_only_claims": sorted(left_claims - right_claims),
        "secondary_only_claims": sorted(right_claims - left_claims),
        "section_comparison": section_comparison,
        "method": (
            "Deterministic structural comparison: identical text does not prove "
            "factual correctness, and different text does not automatically "
            "indicate a substantive conflict."
            if english
            else "確定性結構比較；相同文字不代表事實正確，文字不同也不自動代表實質衝突。"
            if locale == ZH_HANT
            else "确定性结构比较；相同文本不代表事实正确，文本不同也不自动代表实质冲突。"
        ),
    })
    identity = json.dumps(content, ensure_ascii=False, sort_keys=True).encode()
    artifact = ResearchArtifact(
        artifact_id=(
            f"{primary.run_id}:model-comparison:"
            f"{hashlib.sha256(identity).hexdigest()[:10]}"
        ),
        run_id=primary.run_id,
        artifact_type="model-comparison",
        title=("Two-model Research Differences" if english else "雙模型研究分歧" if locale == ZH_HANT else "双模型研究分歧"),
        content=content,
        model_id=f"{snapshot['primary']['model']} vs {snapshot['secondary']['model']}",
        agent_id="deterministic-comparator",
    )
    attempt = StageAttempt(primary.run_id, stage_id, StageOutcome.COMPLETE,
        diagnostics={"comparison_artifact_id": artifact.artifact_id,
                     "primary_revision_id": snapshot["primary"]["revision_id"],
                     "secondary_revision_id": snapshot["secondary"]["revision_id"]})
    storage.save_comparison_artifact(artifact, snapshot, attempt, cancel_check=cancel_check)
    return artifact
