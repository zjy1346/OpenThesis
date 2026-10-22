import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ModelCallPlan } from "../../types";
import { BudgetGapApproval } from "./BudgetGapApproval";

const plan: ModelCallPlan = {
  plan_digest: "plan-v4",
  max_calls: 4,
  max_input_estimate: 10_000,
  max_output_tokens: 200_000,
  pending_budget_gaps: [
    { gap_id: "gap-growth", stage_id: "growth-opportunities", purpose: "generation", input_estimate: 12_345, output_limit: 65_536, diagnostic_code: "MODEL_RUN_CALL_BUDGET_EXHAUSTED" },
    { gap_id: "gap-verify", stage_id: "counter-analysis", purpose: "semantic_verification", input_estimate: 2_000, output_limit: 4_096, diagnostic_code: "MODEL_RUN_VERIFICATION_BUDGET_EXHAUSTED" },
  ],
};

describe("budget gap approval", () => {
  it("shows each explicit gap and does not approve or run it until clicked", () => {
    const approve = vi.fn(async () => undefined);
    render(<BudgetGapApproval runId="run-1" callPlan={plan} language="en" onApprove={approve} />);

    expect(screen.getByText("Growth opportunities · Generation")).toBeInTheDocument();
    expect(screen.getByText("12,345 input tokens (estimate) · 65,536 output tokens (limit)")).toBeInTheDocument();
    expect(screen.getByText("Counter-analysis · Semantic verification")).toBeInTheDocument();
    expect(screen.getByText(/estimates, not a bill.*may incur provider charges/i)).toBeInTheDocument();
    expect(approve).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Approve additional budget and continue gaps" }));
    expect(approve).toHaveBeenCalledWith("plan-v4", ["gap-growth", "gap-verify"]);
  });

  it("surfaces stale-plan failure and prevents duplicate approval while pending", async () => {
    let finish!: () => void;
    const approve = vi.fn(() => new Promise<void>((_resolve, reject) => {
      finish = () => reject(new Error("MODEL_BUDGET_PLAN_CHANGED"));
    }));
    render(<BudgetGapApproval runId="run-1" callPlan={plan} language="zh-CN" onApprove={approve} />);

    fireEvent.click(screen.getByRole("button", { name: "批准追加预算并继续缺口" }));
    const button = screen.getByRole("button", { name: "正在批准追加预算…" });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(approve).toHaveBeenCalledTimes(1);

    finish();
    expect(await screen.findByText(/计划已变化|请核对当前计划|MODEL_BUDGET_PLAN_CHANGED/)).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("已尝试刷新计划");
    expect(screen.getByText(/规划估算，不是账单/)).toBeInTheDocument();
  });

  it("does not apply an old approval failure to the newly selected run", async () => {
    let finishOldApproval!: () => void;
    const approve = vi.fn(() => new Promise<void>((_resolve, reject) => {
      finishOldApproval = () => reject(new Error("MODEL_BUDGET_PLAN_CHANGED"));
    }));
    const view = render(<BudgetGapApproval runId="run-1" callPlan={plan} language="zh-CN" onApprove={approve} />);

    fireEvent.click(screen.getByRole("button", { name: "批准追加预算并继续缺口" }));
    view.rerender(<BudgetGapApproval runId="run-2" callPlan={plan} language="zh-CN" onApprove={approve} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "批准追加预算并继续缺口" })).toBeEnabled());

    await act(async () => {
      finishOldApproval();
      await Promise.resolve();
    });

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "批准追加预算并继续缺口" })).toBeEnabled();
  });

  it("localizes built-in budget stages in all supported languages", () => {
    const stages = [
      ["accounting-risk-analyst", "会计与风险", "Accounting and risk", "會計與風險"],
      ["source-industry-analysis", "原件行业分析", "Source industry analysis", "原件行業分析"],
      ["source-risk-analysis", "原件风险审查", "Source risk review", "原件風險審查"],
    ] as const;
    const stagedPlan: ModelCallPlan = {
      ...plan,
      pending_budget_gaps: stages.map(([stage_id], index) => ({
        gap_id: `gap-${index}`, stage_id, purpose: "generation", input_estimate: 1, output_limit: 2,
        diagnostic_code: "MODEL_RUN_CALL_BUDGET_EXHAUSTED",
      })),
    };
    const view = render(<BudgetGapApproval runId="run-1" callPlan={stagedPlan} language="zh-CN" onApprove={vi.fn()} />);
    for (const [, zhCn, ,] of stages) expect(view.container).toHaveTextContent(zhCn);

    view.rerender(<BudgetGapApproval runId="run-1" callPlan={stagedPlan} language="en" onApprove={vi.fn()} />);
    for (const [, , en,] of stages) expect(view.container).toHaveTextContent(en);

    view.rerender(<BudgetGapApproval runId="run-1" callPlan={stagedPlan} language="zh-Hant" onApprove={vi.fn()} />);
    for (const [, , , zhHant] of stages) expect(view.container).toHaveTextContent(zhHant);
    expect(view.container).not.toHaveTextContent(/accounting-risk-analyst|source-industry-analysis|source-risk-analysis/);
  });

  it("does not render an approval action when no budget gap is pending", () => {
    const view = render(<BudgetGapApproval runId="run-1" callPlan={{ ...plan, pending_budget_gaps: [] }} language="en" onApprove={vi.fn()} />);
    expect(view.container).toBeEmptyDOMElement();
  });
});
