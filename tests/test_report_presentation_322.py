from __future__ import annotations

from openthesis.report_document import assemble_report_document, render_markdown


def _binglun_sample_report() -> dict[str, object]:
    """Minimal user-facing shape copied from the saved 3.2.1 Binglun report."""
    return {
        "growth_opportunities": [
            {
                "title": "顿汉布什中央空调全球化产能释放与海外高毛利工程拓展",
                "mechanism": "依托顿汉布什机组技术与海外品牌影响力，扩大工程系统集成份额。",
                "evidence_grade": "A",
                "probability_range": [0.6, 0.75],
                "scenario_eligibility": ["base", "bull"],
                "supporting_evidence_ids": ["filing:ebd9e5535785e6d05b78"],
                "contradicting_evidence_ids": ["filing:870675098d63bbd2566f"],
                "time_horizon_years": 3,
            }
        ],
        "source_risk_review": [
            {"body": "acknowledged", "evidence_ids": [], "role": "analysis"},
            {
                "body": "年报未披露长账龄应收账款的期后回款明细；现有证据不足以判断最终可回收金额。",
                "evidence_ids": ["filing:870675098d63bbd2566f"],
                "role": "analysis",
            },
        ],
        "valuation_and_sensitivity": {
            "currency": "CNY",
            "limitations": "合并经营现金流与母公司资本开支口径存在差异；敏感性结果仅作情景压力测试。",
            "sensitivity": [
                {"equity_value": 7_137_566_873.898526, "fcf_growth": -0.05},
                {"equity_value": 8_906_232_507.796612, "fcf_growth": 0.0},
                {"equity_value": 29_217_258_513.1457, "fcf_growth": 0.3},
            ],
        },
    }


def _sample_artifacts() -> list[dict[str, object]]:
    return [{
        "artifact_id": "report-final",
        "artifact_type": "research-report",
        "content": {
            "report": _binglun_sample_report(),
            "verification": {"passed": False, "claim_count": 6, "verified_claim_count": 6},
            "readiness": {"state": "substantive_partial", "complete": False},
        },
    }]


def test_binglun_sample_growth_and_sensitivity_use_reader_facing_layout() -> None:
    markdown = render_markdown(
        assemble_report_document("binglun-321-presentation", _sample_artifacts(), language="zh-CN")
    )

    assert "增长机制" in markdown
    assert "依托顿汉布什机组技术与海外品牌影响力，扩大工程系统集成份额。" in markdown
    assert "基准情景" in markdown
    assert "乐观情景" in markdown
    assert "**适用情景：** 基准情景, 乐观情景" in markdown
    assert "scenario_eligibility" not in markdown
    assert "evidence_grade" not in markdown
    assert "其他：A" not in markdown
    assert "A · 强直接证据" in markdown
    assert "| 自由现金流增速 | 权益价值 |" in markdown
    assert "| -5.0% | ¥7.14B |" in markdown
    assert "| 30.0% | ¥29.22B |" in markdown
    assert "敏感性分析[0]" not in markdown
    assert "敏感性分析[" not in markdown
    assert "7137566873.898526" not in markdown
    assert "合并经营现金流与母公司资本开支口径存在差异" in markdown


def test_internal_acknowledgement_receipt_is_hidden_but_real_risk_analysis_remains() -> None:
    markdown = render_markdown(
        assemble_report_document("binglun-321-risk", _sample_artifacts(), language="zh-CN")
    )

    assert "acknowledged" not in markdown
    assert "年报未披露长账龄应收账款的期后回款明细" in markdown
    assert "现有证据不足以判断最终可回收金额" in markdown


def test_research_process_groups_only_equal_stage_and_recorded_status() -> None:
    def checkpoint(stage_id: str, state: str, ordinal: int) -> dict[str, object]:
        return {
            "artifact_id": f"{stage_id}-{state}-{ordinal}",
            "artifact_type": "stage-checkpoint",
            "title": stage_id,
            "content": {
                "result": {
                    "stage_id": stage_id,
                    "validation": {"state": state},
                }
            },
        }

    stage_artifacts = [
        *(checkpoint("financial-analyst", "completed_partial", index) for index in range(125)),
        checkpoint("business-analyst", "completed_partial", 125),
        checkpoint("business-analyst", "completed_partial", 126),
        checkpoint("financial-analyst", "completed_verified", 127),
        checkpoint("business-analyst", "completed_verified", 128),
        {
            "artifact_id": "source-risk-analysis-validation-failed",
            "artifact_type": "stage-checkpoint",
            "title": "source-risk-analysis",
            "content": {
                "result": {
                    "stage_id": "source-risk-analysis",
                    "validation": {"passed": False},
                }
            },
        },
    ]
    artifacts = [*stage_artifacts, *_sample_artifacts()]
    document = assemble_report_document(
        "binglun-321-process", artifacts, language="zh-CN", run_status="partial"
    )
    process = next(section for section in document.sections if section.section_id == "research-process")
    process_lines = [node.text for node in process.nodes]
    markdown = render_markdown(document)

    assert sum("financial-analyst" in line for line in process_lines) == 2
    assert sum("business-analyst" in line for line in process_lines) == 2
    assert any("financial-analyst" in line and "部分完成" in line for line in process_lines)
    assert any("financial-analyst" in line and "已核验" in line for line in process_lines)
    assert any("source-risk-analysis" in line and "核验未通过" in line for line in process_lines)
    assert not document.readiness.complete
    assert "是否通过：否" in markdown
