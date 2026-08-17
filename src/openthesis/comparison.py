from __future__ import annotations

import hashlib
import json
from typing import Any

from .domain import ResearchArtifact, ResearchRun
from .i18n import EN, ZH_HANT, normalize_language
from .storage import Storage


def _final_report(artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    for artifact in reversed(artifacts):
        if artifact["artifact_type"] == "research-report":
            content = artifact["content"]
            report = content.get("report", content)
            return report if isinstance(report, dict) else {"narrative": str(report)}
    return {}


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
) -> ResearchArtifact:
    locale = normalize_language(language)
    english = locale == EN
    left = _final_report(storage.get_artifacts(primary.run_id))
    right = _final_report(storage.get_artifacts(secondary.run_id))
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
        "primary": {
            "run_id": primary.run_id,
            "provider": primary.provider_id,
            "model": primary.model_id,
        },
        "secondary": {
            "run_id": secondary.run_id,
            "provider": secondary.provider_id,
            "model": secondary.model_id,
        },
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
    }
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
        model_id=f"{primary.model_id} vs {secondary.model_id}",
        agent_id="deterministic-comparator",
    )
    storage.save_artifact(artifact)
    return artifact
