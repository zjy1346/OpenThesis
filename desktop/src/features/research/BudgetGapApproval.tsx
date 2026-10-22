import { useEffect, useRef, useState } from "react";
import type { ModelCallPlan, PendingBudgetGap } from "../../types";
import { agentDisplayName } from "./researchProgress";

const COPY = {
  en: {
    title: "Research gaps paused by the call budget",
    input: "input tokens (estimate)", output: "output tokens (limit)",
    estimateWarning: "These are planning estimates, not a bill. Continuing may incur provider charges.",
    approve: "Approve additional budget and continue gaps", approving: "Approving additional budget…",
    stale: "The plan changed or approval did not complete. Saved report content is preserved; a plan refresh was attempted. Review the current plan before trying again.",
    failed: "The added budget or gap recovery failed. Saved report content is preserved.",
  },
  "zh-CN": {
    title: "调用预算暂停的待研究缺口",
    input: "输入 tokens（估算）", output: "输出 tokens（上限）",
    estimateWarning: "这些是规划估算，不是账单；继续可能产生供应商费用。",
    approve: "批准追加预算并继续缺口", approving: "正在批准追加预算…",
    stale: "计划已变化或审批未完成。现有报告正文已保留；已尝试刷新计划，请核对当前计划后再尝试。",
    failed: "追加预算或缺口恢复失败，现有报告正文已保留。",
  },
  "zh-Hant": {
    title: "呼叫預算暫停的待研究缺口",
    input: "輸入 tokens（估算）", output: "輸出 tokens（上限）",
    estimateWarning: "這些是規劃估算，不是帳單；繼續可能產生供應商費用。",
    approve: "批准追加預算並繼續缺口", approving: "正在批准追加預算…",
    stale: "計畫已變更或審批未完成。現有報告正文已保留；已嘗試刷新計畫，請核對目前計畫後再嘗試。",
    failed: "追加預算或缺口恢復失敗，現有報告正文已保留。",
  },
} as const;

function localized(language: string): (typeof COPY)[keyof typeof COPY] {
  return COPY[language === "en" ? "en" : language === "zh-Hant" ? "zh-Hant" : "zh-CN"];
}

function number(value: number | null | undefined, language: string): string {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value.toLocaleString(language === "en" ? "en-US" : "zh-CN")
    : language === "en" ? "Unknown" : "未知";
}

function stageLabel(stageId: string, language: string): string {
  const builtinAgentIds: Record<string, string> = {
    "growth-opportunities": "growth-opportunity-analyst",
    "counter-analysis": "skeptical-analyst",
    "forecast-scenarios": "forecast-analyst",
    "final-synthesis": "research-synthesizer",
    "business-analyst": "business-analyst",
    "financial-analyst": "financial-analyst",
    "accounting-risk-analyst": "accounting-risk-analyst",
    "source-industry-analysis": "source-industry-analysis",
    "source-risk-analysis": "source-risk-analysis",
    "growth-opportunity-analyst": "growth-opportunity-analyst",
    "skeptical-analyst": "skeptical-analyst",
    "forecast-analyst": "forecast-analyst",
    "research-synthesizer": "research-synthesizer",
  };
  const agentId = builtinAgentIds[stageId];
  if (agentId) return agentDisplayName(language, agentId);
  const labels: Record<string, readonly [string, string, string]> = {
    "growth-opportunities": ["增长机会", "Growth opportunities", "成長機會"],
    "counter-analysis": ["反方分析", "Counter-analysis", "反方分析"],
    "forecast-scenarios": ["经营情景", "Operating scenarios", "營運情境"],
    "final-synthesis": ["最终综合", "Final synthesis", "最終綜合"],
    "business-analyst": ["业务分析", "Business analysis", "業務分析"],
    "financial-analyst": ["财务分析", "Financial analysis", "財務分析"],
    "research-synthesizer": ["研究综合", "Research synthesis", "研究綜合"],
  };
  const values = labels[stageId];
  return values ? values[language === "en" ? 1 : language === "zh-Hant" ? 2 : 0] : stageId;
}

function purposeLabel(purpose: string, language: string): string {
  const labels: Record<string, readonly [string, string, string]> = {
    generation: ["生成", "Generation", "生成"],
    semantic_verification: ["语义核验", "Semantic verification", "語意核驗"],
    claim_repair: ["主张修复", "Claim repair", "主張修復"],
    field_repair: ["字段修复", "Field repair", "欄位修復"],
    output_repair: ["输出修复", "Output repair", "輸出修復"],
    format_repair: ["格式修复", "Format repair", "格式修復"],
    section_delivery: ["分章交付", "Section delivery", "分章交付"],
    material_coverage: ["材料覆盖", "Material coverage", "材料覆蓋"],
  };
  const values = labels[purpose];
  return values ? values[language === "en" ? 1 : language === "zh-Hant" ? 2 : 0] : purpose;
}

function errorCopy(reason: unknown, copy: (typeof COPY)[keyof typeof COPY]): string {
  const detail = reason instanceof Error ? reason.message : String(reason ?? "");
  const code = detail.match(/MODEL_BUDGET_[A-Z_]+/)?.[0];
  const text = code === "MODEL_BUDGET_PLAN_CHANGED" || code === "MODEL_BUDGET_GAP_CHANGED"
    ? copy.stale : copy.failed;
  return code ? `${text} (${code})` : text;
}

export function BudgetGapApproval({ runId, callPlan, language, onApprove, disabled = false }: {
  runId: string;
  callPlan?: ModelCallPlan | null;
  language: string;
  onApprove?: (planDigest: string, gapIds: string[]) => Promise<void>;
  disabled?: boolean;
}) {
  const [approving, setApproving] = useState(false);
  const [error, setError] = useState("");
  const approvalGeneration = useRef(0);
  const copy = localized(language);
  const gaps: PendingBudgetGap[] = Array.isArray(callPlan?.pending_budget_gaps)
    ? callPlan.pending_budget_gaps.filter((gap) => gap && typeof gap.gap_id === "string" && gap.gap_id)
    : [];

  useEffect(() => {
    approvalGeneration.current += 1;
    setError("");
    setApproving(false);
    return () => { approvalGeneration.current += 1; };
  }, [runId]);
  if (!callPlan?.plan_digest || gaps.length === 0) return null;

  const approve = async () => {
    if (!onApprove || approving || disabled) return;
    const generation = approvalGeneration.current;
    setApproving(true);
    setError("");
    try {
      await onApprove(callPlan.plan_digest, gaps.map((gap) => gap.gap_id));
    } catch (reason) {
      if (generation === approvalGeneration.current) setError(errorCopy(reason, copy));
    } finally {
      if (generation === approvalGeneration.current) setApproving(false);
    }
  };

  return <section className="budget-gap-approval" aria-label={copy.title}>
    <div className="budget-gap-heading">
      <div>
        <strong>{copy.title}</strong>
        <p>{copy.estimateWarning}</p>
      </div>
      <button type="button" className="budget-gap-approve" disabled={!onApprove || disabled || approving}
        onClick={() => void approve()}>
        {approving ? copy.approving : copy.approve}
      </button>
    </div>
    <ul className="budget-gap-list">
      {gaps.map((gap) => <li key={gap.gap_id}>
        <strong>{stageLabel(gap.stage_id, language)} · {purposeLabel(gap.purpose, language)}</strong>
        <span>{number(gap.input_estimate, language)} {copy.input} · {number(gap.output_limit, language)} {copy.output}</span>
      </li>)}
    </ul>
    {error && <p className="budget-gap-error" role="alert">{error}</p>}
  </section>;
}
