import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { openExternalUrl } from "../../backend";
import { ResearchTaskCenter, type ResearchTaskCenterProps } from "./ResearchTaskCenter";
import type { ResearchJob, ResearchTaskSummary } from "../../types";

vi.mock("../../backend", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../backend")>(),
  openExternalUrl: vi.fn().mockResolvedValue(undefined),
}));

const counts = { running: 1, queued: 1, waiting_input: 0, interrupted: 0, cancelling: 0 };

function task(jobId: string, state: ResearchTaskSummary["state"] = "running"): ResearchTaskSummary {
  return {
    job_id: jobId,
    run_id: `run-${jobId}`,
    state,
    revision: 4,
    operation_type: "research",
    company: { name: `Company ${jobId}`, ticker: jobId.toUpperCase() },
    model: "model:test",
    stage: "synthesis",
    message: "Working",
    elapsed_seconds: 62,
    created_at: "2026-10-28T10:00:00Z",
    queue_position: state === "queued" ? 1 : undefined,
    ahead_count: state === "queued" ? 1 : undefined,
  };
}

function detail(jobId: string, state: ResearchJob["state"] = "running", patch: Partial<ResearchJob> = {}): ResearchJob {
  return {
    job_id: jobId,
    run_id: `run-${jobId}`,
    state,
    revision: 4,
    operation_type: "research",
    company: { name: `Company ${jobId}`, ticker: jobId.toUpperCase() },
    model: "model:test",
    message: "Working",
    percent: 35,
    stage: "synthesis",
    elapsed_seconds: 62,
    result_access: state === "completed" ? "saved" : state === "waiting_input" || state === "interrupted" ? "recovery" : "none",
    ...patch,
  };
}

type Scenario = Partial<ResearchTaskCenterProps> & {
  items?: ResearchTaskSummary[];
  onCancel?: ResearchTaskCenterProps["actions"]["cancel"];
  onResume?: ResearchTaskCenterProps["actions"]["resume"];
  onArchive?: ResearchTaskCenterProps["actions"]["archive"];
  onViewReport?: (runId: string | null) => void;
  onVisionDecision?: ResearchTaskCenterProps["actions"]["decideVision"];
};
function props({
  items = [task("a"), task("b"), task("c", "queued")],
  onCancel = vi.fn(), onResume = vi.fn(), onArchive = vi.fn(), onViewReport = vi.fn(),
  onVisionDecision = vi.fn(), ...overrides
}: Scenario = {}): ResearchTaskCenterProps {
  return {
    language: "en",
    catalog: { filter: "all", items, loading: false, error: "", hasMore: false, hasPrevious: false, pageNumber: 1,
      selectFilter: vi.fn(), refresh: vi.fn(), nextPage: vi.fn(), previousPage: vi.fn() },
    counts, queuePaused: false, queuePending: false, queueError: "", onContinueQueue: vi.fn(),
    selectedTaskId: null, detail: null, detailError: "", actionPending: false, actionError: "",
    onSelect: vi.fn(), onReloadDetail: vi.fn(), openDetailToken: 0, models: [],
    actions: { cancel: onCancel, resume: onResume, archive: onArchive, openResult: (job) => onViewReport(job.run_id),
      decideVision: onVisionDecision, configureModels: vi.fn(), cancelModel: vi.fn(), approveBudget: vi.fn(),
      loadCallHistory: vi.fn() },
    ...overrides,
  };
}

function openTechnicalDetails(language: ResearchTaskCenterProps["language"] = "en") {
  const labels = language === "en" ? ["Diagnostics", "Diagnostic code"] : language === "zh-Hant" ? ["診斷", "診斷代碼"] : ["诊断", "诊断代码"];
  fireEvent.click(screen.getByRole("tab", { name: labels[0] }));
  fireEvent.click(within(screen.getByRole("tabpanel")).getByText(labels[1], { selector: "summary" }));
}

describe("ResearchTaskCenter", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows A/B/C states but renders heavy progress only for the selected task", () => {
    const view = render(<ResearchTaskCenter {...props()} />);
    expect(screen.getByText("Company a")).toBeInTheDocument();
    expect(screen.getByText("Company b")).toBeInTheDocument();
    expect(screen.getByText("Company c")).toBeInTheDocument();
    expect(screen.getAllByText("Running").length).toBeGreaterThan(0);
    expect(screen.getByText("Queued")).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Overview" })).not.toBeInTheDocument();

    view.rerender(<ResearchTaskCenter {...props({ selectedTaskId: "b", detail: detail("b") })} />);
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("heading", { name: "Company b" })).toBeVisible();
    expect(screen.queryByText("35%")).not.toBeInTheDocument();
  });

  it("binds cancellation to selected detail B, not the other listed jobs", () => {
    const onCancel = vi.fn();
    render(<ResearchTaskCenter {...props({ selectedTaskId: "b", detail: detail("b"), onCancel })} />);
    fireEvent.click(screen.getByRole("button", { name: "Cancel task" }));
    expect(onCancel).toHaveBeenCalledExactlyOnceWith("b");
  });

  it("does not resume an unknown remote result in waiting_input and keeps cancel and report recovery scoped", () => {
    const onResume = vi.fn();
    const onViewReport = vi.fn();
    const onCancel = vi.fn();
    const job = detail("unknown", "waiting_input", {
      revision: 9,
      blocked_reason: "MODEL_REMOTE_RESULT_UNKNOWN",
    });
    render(<ResearchTaskCenter {...props({
      items: [task("unknown", "waiting_input")], selectedTaskId: "unknown", detail: job,
      onResume, onViewReport, onCancel,
    })} />);
    expect(screen.getByText(/unknown remote outcome/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Continue task" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel task" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open report recovery" }));
    expect(onViewReport).toHaveBeenCalledExactlyOnceWith("run-unknown");
    fireEvent.click(screen.getByRole("button", { name: "Cancel task" }));
    expect(onCancel).toHaveBeenCalledExactlyOnceWith("unknown");
    expect(onResume).not.toHaveBeenCalled();
    expect(screen.getAllByText(/unknown remote outcome/i)).toHaveLength(1);
    expect(screen.queryByText("MODEL_REMOTE_RESULT_UNKNOWN")).not.toBeInTheDocument();
  });

  it("binds an approval decision to job, detail revision, and plan id", () => {
    const onVisionDecision = vi.fn();
    const job = detail("waiting", "waiting_input", {
      revision: 12,
      vision_approval_pending: true,
      vision_upload_preview: { plan_id: "plan-7", provider: "vision:test", pages: [2], total_bytes: 2048 },
    });
    render(<ResearchTaskCenter {...props({
      items: [task("waiting", "waiting_input")], selectedTaskId: "waiting", detail: job, onVisionDecision,
    })} />);
    fireEvent.click(screen.getByRole("button", { name: "Approve upload" }));
    expect(onVisionDecision).toHaveBeenCalledExactlyOnceWith("waiting", 12, "plan-7", true);
  });

  it("requires an explicit replacement model before continuing a configuration failure", () => {
    const onResume = vi.fn();
    const model = { configured_model_id: "model:new", configuration_version: 3, connection_id: "connection-2", alias: "New model" };
    render(<ResearchTaskCenter {...props({
      items: [task("config", "interrupted")],
      selectedTaskId: "config",
      detail: detail("config", "interrupted", { revision: 6, blocked_reason: "MODEL_CONFIGURATION_CHANGED" }),
      models: [model],
      onResume,
    })} />);
    const resume = screen.getByRole("button", { name: "Change model and continue" });
    expect(resume).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Choose model configuration"), { target: { value: "model:new:3:connection-2" } });
    expect(resume).toBeEnabled();
    fireEvent.click(resume);
    expect(onResume).toHaveBeenCalledExactlyOnceWith("config", 6, {
      configured_model_id: "model:new", configuration_version: 3, connection_id: "connection-2", role: "primary",
    });
  });

  it("recognizes gateway configuration failures and translates operation and stage keys", () => {
    const job = detail("gateway", "interrupted", { blocked_reason: "MODEL_GATEWAY_CONFIGURATION_ERROR" });
    render(<ResearchTaskCenter {...props({
      items: [task("gateway", "interrupted"), { ...task("financial", "queued"), operation_type: "financial_retry" }],
      selectedTaskId: "gateway",
      detail: job,
      models: [{ configured_model_id: "replacement", configuration_version: 1, alias: "Replacement" }],
    })} />);

    expect(screen.getByText(/Financial retry/)).toBeInTheDocument();
    expect(screen.getAllByText("Task interrupted; progress saved").length).toBeGreaterThan(0);
    expect(screen.getByText("The model configuration could not be checked. Review the model connection before continuing.")).toBeInTheDocument();
    expect(screen.getByLabelText("Choose model configuration")).toBeInTheDocument();
    expect(screen.queryByText("MODEL_GATEWAY_CONFIGURATION_ERROR")).not.toBeInTheDocument();
    openTechnicalDetails();
    expect(screen.getByText("MODEL_GATEWAY_CONFIGURATION_ERROR")).toBeVisible();
  });

  it.each([
    ["zh-CN", "任务已中断，进度已保留", "模型配置检查未能完成"],
    ["zh-Hant", "任務已中斷，進度已保留", "模型設定檢查未能完成"],
    ["en", "Task interrupted; progress saved", "The model configuration could not be checked"],
  ] as const)("localizes stage and configuration reason instead of showing raw keys in %s", (language, stageTitle, reason) => {
    render(<ResearchTaskCenter {...props({
      language,
      items: [{ ...task("localized", "interrupted"), stage: "filing-validation" }],
      selectedTaskId: "localized",
      detail: detail("localized", "interrupted", { stage: "filing-validation", blocked_reason: "MODEL_GATEWAY_CONFIGURATION_ERROR" }),
      models: [{ configured_model_id: "replacement", configuration_version: 1, alias: "Replacement" }],
    })} />);
    expect(screen.getAllByText(stageTitle).length).toBeGreaterThan(0);
    expect(screen.getByText(new RegExp(reason))).toBeInTheDocument();
    expect(screen.queryByText("MODEL_GATEWAY_CONFIGURATION_ERROR", { selector: ".research-task-center__reason span" })).not.toBeInTheDocument();
  });

  it.each([
    ["zh-CN", ["财报重试", "财报重建", "模型恢复", "报告刷新", "重新校验已保存数据"]],
    ["zh-Hant", ["財報重試", "財報重建", "模型恢復", "報告重新整理", "重新驗證已儲存資料"]],
    ["en", ["Financial retry", "Financial rebuild", "Model recovery", "Report refresh", "Revalidate saved data"]],
  ] as const)("localizes backend operation keys in %s", (language, operations) => {
    const operationTypes = ["financial_retry", "financial_rebuild", "model_recovery", "report_refresh", "revalidate_saved"];
    render(<ResearchTaskCenter {...props({
      language,
      items: operationTypes.map((operation_type, index) => ({ ...task(`operation-${index}`, "queued"), operation_type })),
    })} />);
    for (const operation of operations) expect(screen.getByText(new RegExp(operation))).toBeInTheDocument();
  });

  it.each([
    ["zh-CN", "上次模型请求的远端结果未知", "打开报告恢复入口", "继续任务"],
    ["zh-Hant", "上次模型請求的遠端結果未知", "開啟報告恢復入口", "繼續任務"],
    ["en", "unknown remote outcome", "Open report recovery", "Continue task"],
  ] as const)("recognizes MODEL_REMOTE_RESULT_UNKNOWN while waiting_input and never offers generic resume in %s", (language, summary, recovery, resume) => {
    const onResume = vi.fn();
    const job = detail("unknown-localized", "waiting_input", { blocked_reason: "MODEL_REMOTE_RESULT_UNKNOWN" });
    render(<ResearchTaskCenter {...props({
      language,
      items: [task("unknown-localized", "waiting_input")],
      selectedTaskId: "unknown-localized",
      detail: job,
      onResume,
    })} />);
    expect(screen.getByRole("alert")).toHaveTextContent(summary);
    expect(screen.getByRole("button", { name: recovery })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: resume })).not.toBeInTheDocument();
    expect(onResume).not.toHaveBeenCalled();
  });

  it.each([
    ["recovery diagnostic", { recovery_outcome: { state: "unknown", diagnostic_code: "MODEL_REMOTE_RESULT_UNKNOWN", next_actions: ["open_report_recovery"] as string[] } }],
    ["error code", { error_code: "MODEL_REMOTE_RESULT_UNKNOWN" }],
  ] as const)("blocks generic continuation when an unknown remote result is in the %s despite another blocked reason", (_source, patch) => {
    const onResume = vi.fn();
    render(<ResearchTaskCenter {...props({
      items: [task("unknown-source", "waiting_input")],
      selectedTaskId: "unknown-source",
      detail: detail("unknown-source", "waiting_input", { blocked_reason: "ANOTHER_BLOCKING_REASON", ...patch }),
      onResume,
    })} />);

    expect(screen.getByRole("alert")).toHaveTextContent(/unknown remote outcome/i);
    expect(screen.queryByRole("button", { name: "Continue task" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel task" })).toBeInTheDocument();
    expect(onResume).not.toHaveBeenCalled();
  });

  it.each(["", "   "]) ("uses a readable fallback for a %s waiting reason", (blockedReason) => {
    render(<ResearchTaskCenter {...props({
      items: [task("blank-reason", "waiting_input")],
      selectedTaskId: "blank-reason",
      detail: detail("blank-reason", "waiting_input", { blocked_reason: blockedReason }),
    })} />);

    expect(screen.getByText("This task needs review before it can continue.")).toBeInTheDocument();
  });

  it.each([
    ["zh-CN", "没有可用的官方财务报告", "打开官方披露页面"],
    ["zh-Hant", "目前沒有可用的官方財務報告", "開啟官方披露頁面"],
    ["en", "No usable official financial reports", "Open official disclosure page"],
  ] as const)("shows readable localized outcome and safe official disclosure action in %s", (language, noFilings, openDisclosure) => {
    const job = detail("no-filings", "failed", {
      error_code: "NO_FILINGS_AVAILABLE",
      message: "NO_FILINGS_AVAILABLE",
      disclosure_url: "https://www.cninfo.com.cn/new/index",
    });
    render(<ResearchTaskCenter {...props({
      language,
      items: [task("no-filings", "failed")],
      selectedTaskId: "no-filings",
      detail: job,
    })} />);
    expect(screen.getByText(new RegExp(noFilings))).toBeVisible();
    openTechnicalDetails(language);
    expect(screen.getByText("NO_FILINGS_AVAILABLE")).toBeVisible();
    expect(screen.queryByText("The research is not complete. Review the report's missing data and evidence before deciding what to do next.")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: openDisclosure }));
    expect(openExternalUrl).toHaveBeenCalledWith("https://www.cninfo.com.cn/new/index");
  });

  it("reports attempt completion separately from incomplete research and opens report recovery without restarting", () => {
    const onResume = vi.fn();
    const onViewReport = vi.fn();
    const job = detail("incomplete", "completed", {
      attempt_complete: true,
      research_complete: false,
      action_required: true,
      error_code: "FILING_DATA_QUALITY_FAILED",
      message: "FILING_DATA_QUALITY_FAILED",
      recovery_outcome: { state: "needs_action", diagnostic_code: "FINANCIAL_DATA_QUALITY_FAILED", next_actions: ["open_report_recovery"] },
    });
    render(<ResearchTaskCenter {...props({
      items: [task("incomplete", "completed")],
      selectedTaskId: "incomplete",
      detail: job,
      onResume,
      onViewReport,
    })} />);

    expect(screen.getByText("Partial result")).toBeVisible();
    expect(screen.getByText(/Financial data does not yet meet the research requirements/)).toBeVisible();
    openTechnicalDetails();
    expect(screen.getByText("FINANCIAL_DATA_QUALITY_FAILED")).toBeVisible();
    expect(screen.getByRole("button", { name: "View saved research" })).toBeVisible();
    expect(screen.queryByText(/research is complete/i)).not.toBeInTheDocument();
    expect(onResume).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "View saved research" }));
    expect(onViewReport).toHaveBeenCalledExactlyOnceWith("run-incomplete");
  });

  it("shows explicit research completion and required action without marking the research incomplete", () => {
    render(<ResearchTaskCenter {...props({
      items: [task("complete-action", "completed")],
      selectedTaskId: "complete-action",
      detail: detail("complete-action", "completed", {
        attempt_complete: true,
        research_complete: true,
        action_required: true,
        error_code: "POST_RESEARCH_ACTION_REQUIRED",
      }),
    })} />);

    expect(screen.getByRole("button", { name: "View research report" })).toBeVisible();
    expect(screen.getByText("The research is complete.")).toBeInTheDocument();
    expect(screen.getByText("Further action is required for this task.")).toBeInTheDocument();
    expect(screen.queryByText(/research is not complete/i)).not.toBeInTheDocument();
  });

  it("does not infer research completeness from an ended attempt or required action", () => {
    render(<ResearchTaskCenter {...props({
      items: [task("unknown-completion", "completed")],
      selectedTaskId: "unknown-completion",
      detail: detail("unknown-completion", "completed", {
        attempt_complete: true,
        action_required: true,
      }),
    })} />);

    expect(screen.getByRole("button", { name: "View saved research" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "View research report" })).not.toBeInTheDocument();
    expect(screen.queryByText("The research is complete.")).not.toBeInTheDocument();
  });

  it("does not expose untrusted disclosure URLs", () => {
    render(<ResearchTaskCenter {...props({
      items: [task("unsafe", "failed")], selectedTaskId: "unsafe",
      detail: detail("unsafe", "failed", { error_code: "NO_FILINGS_AVAILABLE", disclosure_url: "https://evil.example/filing" }),
    })} />);
    expect(screen.queryByRole("button", { name: "Open official disclosure page" })).not.toBeInTheDocument();
    expect(openExternalUrl).not.toHaveBeenCalled();
  });

  it.each(["waiting_input", "interrupted"] as const)("allows an explicit cancellation for %s tasks", (state) => {
    const onCancel = vi.fn();
    render(<ResearchTaskCenter {...props({
      items: [task("action-needed", state)], selectedTaskId: "action-needed",
      detail: detail("action-needed", state), onCancel,
    })} />);
    fireEvent.click(screen.getByRole("button", { name: "Cancel task" }));
    expect(onCancel).toHaveBeenCalledExactlyOnceWith("action-needed");
  });

  it("still offers cancellation when an upload approval is missing its plan", () => {
    const onCancel = vi.fn();
    render(<ResearchTaskCenter {...props({
      items: [task("missing-plan", "waiting_input")], selectedTaskId: "missing-plan",
      detail: detail("missing-plan", "waiting_input", { vision_approval_pending: true, vision_upload_preview: null }),
      onCancel,
    })} />);
    fireEvent.click(screen.getByRole("button", { name: "Cancel task" }));
    expect(onCancel).toHaveBeenCalledExactlyOnceWith("missing-plan");
  });

  it("does not describe normally running research as a repair outcome", () => {
    render(<ResearchTaskCenter {...props({
      items: [task("running", "running")], selectedTaskId: "running",
      detail: detail("running", "running", { attempt_complete: false, research_complete: false, action_required: false }),
    })} />);
    expect(screen.queryByText(/The research is not complete/)).not.toBeInTheDocument();
    expect(screen.queryByText("Task result and next step")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel task" })).toBeInTheDocument();
  });

  it("does not offer archive for active jobs", () => {
    const onArchive = vi.fn();
    render(<ResearchTaskCenter {...props({ selectedTaskId: "active", detail: detail("active"), onArchive })} />);
    expect(screen.queryByRole("button", { name: "Archive task" })).not.toBeInTheDocument();
    expect(onArchive).not.toHaveBeenCalled();
  });

  it("keeps a selected task and its tab when that task leaves the current filter", () => {
    const base = props({ selectedTaskId: "b", detail: detail("b") });
    const catalog = { ...base.catalog, filter: "active" as const };
    const view = render(<ResearchTaskCenter {...base} catalog={catalog} />);
    fireEvent.click(screen.getByRole("tab", { name: "Preparation" }));
    view.rerender(<ResearchTaskCenter {...base} catalog={{ ...catalog, items: [task("a")] }}
      selectedSummary={task("b", "completed")} detail={detail("b", "completed")} />);
    expect(screen.getByRole("heading", { name: "Company b" })).toBeVisible();
    expect(screen.getByRole("tab", { name: "Preparation" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText(/no longer in the selected filter/)).toBeVisible();
  });

  it("does not expose approval or archived result actions from a previous selection", () => {
    const base = props({ selectedTaskId: "b", detail: detail("b", "waiting_input", {
      vision_approval_pending: true,
      vision_upload_preview: { plan_id: "plan-b", provider: "offline fixture", pages: [3], total_bytes: 2048 },
    }) });
    const view = render(<ResearchTaskCenter {...base} />);
    expect(screen.getByRole("button", { name: "Approve upload" })).toBeVisible();
    view.rerender(<ResearchTaskCenter {...base} selectedTaskId="a" />);
    expect(screen.queryByRole("button", { name: "Approve upload" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Company b" })).not.toBeInTheDocument();
  });

  it("does not offer a saved result based on a run id alone", () => {
    render(<ResearchTaskCenter {...props({ selectedTaskId: "b", detail: detail("b", "cancelled", { result_access: "none" }) })} />);
    expect(screen.queryByRole("button", { name: /View (saved research|research report)/ })).not.toBeInTheDocument();
    expect(screen.getByText("No saved research result is available for this attempt.")).toBeVisible();
  });
});
