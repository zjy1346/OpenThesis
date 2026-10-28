import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ResearchProgress } from "./States";
import type { ResearchJob } from "../types";

describe("ResearchProgress", () => {
  it("estimates only fresh page work and keeps file/page counts separate across both tabs", () => {
    const job: ResearchJob = { job_id: "fresh-pages", state: "running", message: "", percent: 20, run_id: null,
      stage: "filing-page", elapsed_seconds: 3_600,
      filing_states: {
        done: { filing_id: "done", label: "2024", status: "usable", processing_state: "completed", elapsed_seconds: 20 },
        active: { filing_id: "active", label: "2025", status: "page-parsing", elapsed_seconds: 3_500,
          completed_page_count: 103, candidate_page_count: 200, parsed_page_count: 3,
          parsing_active_seconds: 6, parser_backend: "pdfplumber", waiting_type: "native_parse",
          current_page: 137, last_product_at: "2026-10-26T10:00:00+08:00" },
      } };
    const view = render(<ResearchProgress language="en" job={job} onCancel={vi.fn()} />);
    expect(screen.getByLabelText("Estimated remaining")).toHaveTextContent("2m–5m");
    expect(view.container).toHaveTextContent("Filings processed: 1 / 2");
    expect(view.container).toHaveTextContent("Current file candidate pages: 103 / 200");
    fireEvent.click(screen.getByRole("tab", { name: "Model research" }));
    expect(view.container).toHaveTextContent("Current file candidate pages: 103 / 200");
    const activity = view.container.querySelector("details.filing-activity-details")
      ?? [...view.container.querySelectorAll("details")].find((item) => item.textContent?.includes("Current PDF page"));
    expect(activity).not.toHaveAttribute("open");
    expect(activity).toHaveTextContent("Current PDF page: 137");
  });

  it("shows estimating instead of extrapolating percentages or cached pages", () => {
    render(<ResearchProgress language="en" job={{ job_id: "reuse-only", state: "running", message: "", percent: 24,
      run_id: null, stage: "filing-page", elapsed_seconds: 3_600,
      filing_states: { active: { filing_id: "active", label: "2025", status: "page-cache-hit", elapsed_seconds: 3_600,
        completed_page_count: 100, candidate_page_count: 200, parsed_page_count: 0,
        parsing_active_seconds: 0, parser_backend: "pdfplumber", waiting_type: "cache_reuse" } } }} onCancel={vi.fn()} />);
    expect(screen.queryByLabelText("Estimated remaining")).not.toBeInTheDocument();
    expect(screen.getByText("Estimating from completed work…")).toBeInTheDocument();
  });

  it.each([
    ["en", "15-minute quality-first reminder", "20 minutes is not a hard stop"],
    ["zh-CN", "15分钟质量优先提示", "20分钟不是硬性终止时限"],
    ["zh-Hant", "15分鐘品質優先提示", "20分鐘不是硬性終止時限"],
  ])("shows the top-level time reminder during financial preparation in %s", (language, reminder, boundary) => {
    render(<ResearchProgress language={language} job={{ job_id: "time-warning-preparation", state: "running", message: "Preparing", percent: 24,
      run_id: null, stage: "filing-validation", call_plan: { plan_digest: "plan", max_calls: 16, max_input_estimate: 1_500_000, max_output_tokens: 100_000,
        pending_budget_gaps: [], time_warning: true } }}
      onCancel={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent(reminder);
    expect(screen.getByRole("status")).toHaveTextContent(boundary);
  });

  it("does not show a time reminder unless the backend explicitly flags the boundary", () => {
    const { rerender } = render(<ResearchProgress language="en" job={{ job_id: "no-time-warning", state: "running", message: "Preparing", percent: 24,
      run_id: null, stage: "filing-validation", call_plan: { plan_digest: "plan", max_calls: 16, max_input_estimate: 1_500_000, max_output_tokens: 100_000,
        pending_budget_gaps: [], time_warning: false } }}
      onCancel={vi.fn()} />);
    expect(screen.queryByText(/15-minute quality-first reminder/)).not.toBeInTheDocument();
    rerender(<ResearchProgress language="en" job={{ job_id: "no-time-warning", state: "running", message: "Preparing", percent: 24,
      run_id: null, stage: "filing-validation" }} onCancel={vi.fn()} />);
    expect(screen.queryByText(/15-minute quality-first reminder/)).not.toBeInTheDocument();
  });

  it("keeps the top-level reminder visible on the research tab before call cards exist", () => {
    render(<ResearchProgress language="en" job={{ job_id: "time-warning-no-calls", state: "running", message: "Preparing", percent: 42,
      run_id: null, stage: "filing-validation", call_plan: { plan_digest: "plan", max_calls: 16, max_input_estimate: 1_500_000,
        max_output_tokens: 100_000, pending_budget_gaps: [], time_warning: true }, model_calls: [] }} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "Model research" }));
    expect(screen.getByRole("status")).toHaveTextContent("15-minute quality-first reminder");
  });

  it("keeps the model-stage reminder on ModelCallProgress without duplicating it", () => {
    render(<ResearchProgress language="en" job={{ job_id: "time-warning-model", state: "running", message: "Researching", percent: 55,
      run_id: null, stage: "parallel-agents", agent_states: { "financial-analyst": "running" },
      call_plan: { plan_digest: "plan", max_calls: 16, max_input_estimate: 1_500_000, max_output_tokens: 100_000,
        pending_budget_gaps: [], time_warning: true },
      model_calls: [{ request_id: "warning-call", attempt_id: "attempt-1", generation: 1, stage_id: "financial-analyst", state: "running", elapsed_seconds: 2,
        call_plan: { max_calls: 16, max_input_estimate: 1_500_000, max_output_tokens: 100_000, time_warning: true } }] }} onCancel={vi.fn()} />);
    expect(screen.getAllByText(/15-minute quality-first reminder/)).toHaveLength(1);
  });

  it("distinguishes source-stage processing and partial verification from final review", () => {
    render(<ResearchProgress language="en" job={{ job_id: "source", state: "running", message: "Reading", percent: 30, run_id: null,
      agent_states: { "source-risk-analysis": "validating", "source-industry-analysis": "completed_partial", "skeptical-analyst": "queued" } }}
      onCancel={vi.fn()} />);
    expect(screen.getByText("Source risk review")).toBeTruthy();
    expect(screen.getByText("Counter-analysis")).toBeTruthy();
    expect(screen.getByText("Processing and verifying")).toBeTruthy();
    expect(screen.getByText("Processed; verification incomplete")).toBeTruthy();
  });
  it("renders localized approval actions and invokes decisions", () => {
    const onCancel = vi.fn();
    const onVisionDecision = vi.fn();
    render(<ResearchProgress job={{ job_id: "vision", state: "running", message: "Review", percent: 10, run_id: null, vision_approval_pending: true, vision_upload_preview: { provider: "mineru_lite", pages: [2, 3], total_bytes: 2048, source_document: "annual.pdf", filing_hash: "abcdef123456" } }} labels={{ cancel: "Cancel", cancelling: "Stopping", agents: "Agents", running: "Running", retrying: "Retrying", queued: "Queued", completed: "Done", cancelled: "Cancelled", failed: "Failed", unknown: "Waiting", visionApprovalTitle: "Approve cloud upload", visionApprovalProvider: "Provider", visionApprovalDocument: "Document", visionApprovalPages: "Pages", visionApprovalSize: "Size", visionApprovalFingerprint: "Fingerprint", visionApprovalApprove: "Approve", visionApprovalDecline: "Decline" }} onCancel={onCancel} onVisionDecision={onVisionDecision} />);
    expect(screen.getByRole("dialog", { name: "Approve cloud upload" })).toBeInTheDocument();
    screen.getByRole("button", { name: "Approve" }).click();
    screen.getByRole("button", { name: "Decline" }).click();
    screen.getByRole("button", { name: "Cancel" }).click();
    expect(onVisionDecision).toHaveBeenNthCalledWith(1, true);
    expect(onVisionDecision).toHaveBeenNthCalledWith(2, false);
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Stopping" })).not.toBeInTheDocument();
  });

  it("shows cancellation acknowledgement and per-agent progress", () => {
    render(
      <ResearchProgress
        job={{
          job_id: "job-1",
          state: "cancelling",
          message: "Stopping unfinished agents…",
          percent: 48,
          run_id: null,
          stage: "parallel-agents",
          agent_states: {
            "financial-analyst": "running",
            "business-analyst": "queued",
          },
          completed_agents: 0,
          total_agents: 3,
          cancel_requested: true,
          elapsed_seconds: 4,
        }}
        labels={{
          cancel: "Cancel research",
          cancelling: "Stopping…",
          agents: "Agents",
          running: "Running",
          retrying: "Retrying separately",
          queued: "Queued",
          completed: "Completed",
          cancelled: "Cancelled",
          failed: "Failed",
          unknown: "Waiting",
        }}
        language="en"
        onCancel={vi.fn()}
      />,
    );

    expect(screen.getByText("Stopping the research safely…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stopping…" })).toBeDisabled();
    expect(screen.getByText("Financial analysis")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.getByText("Queued")).toBeInTheDocument();
  });

  it("counts elapsed research time forward and syncs to a newer backend value", () => {
    vi.useFakeTimers();
    const props = {
      job: { job_id: "timer", state: "running" as const, message: "", percent: 18, run_id: null, stage: "filing-parse", elapsed_seconds: 4 },
      labels: { cancel: "Cancel", cancelling: "Stopping", agents: "Agents", running: "Running", retrying: "Retrying", queued: "Queued", completed: "Done", cancelled: "Cancelled", failed: "Failed", unknown: "Waiting" },
      language: "en",
      onCancel: vi.fn(),
    };
    const view = render(<ResearchProgress {...props} />);
    expect(screen.getByLabelText("Elapsed research time")).toHaveTextContent("00:04");
    act(() => vi.advanceTimersByTime(2_000));
    expect(screen.getByLabelText("Elapsed research time")).toHaveTextContent("00:06");
    view.rerender(<ResearchProgress {...props} job={{ ...props.job, elapsed_seconds: 12 }} />);
    expect(screen.getByLabelText("Elapsed research time")).toHaveTextContent("00:12");
    view.unmount();
    vi.useRealTimers();
  });

  it("uses actual elapsed time after a background-tab pause", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-16T00:00:00Z"));
    const view = render(
      <ResearchProgress
        job={{ job_id: "background", state: "running", message: "", percent: 24, run_id: null, stage: "filing-validation", elapsed_seconds: 8 }}
        labels={{ cancel: "Cancel", cancelling: "Stopping", agents: "Agents", running: "Running", retrying: "Retrying", queued: "Queued", completed: "Done", cancelled: "Cancelled", failed: "Failed", unknown: "Waiting" }}
        language="en"
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getByLabelText("Elapsed research time")).toHaveTextContent("00:08");
    vi.setSystemTime(new Date("2026-08-16T00:00:37Z"));
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    expect(screen.getByLabelText("Elapsed research time")).toHaveTextContent("00:45");
    view.unmount();
    vi.useRealTimers();
  });

  it("localizes filing statuses and shows a bounded remaining range", () => {
    render(
      <ResearchProgress
        language="en"
        job={{
          job_id: "eta",
          state: "running",
          message: "",
          percent: 42,
          run_id: null,
          stage: "filing-validation",
          stage_current: 2,
          stage_total: 5,
          elapsed_seconds: 30,
          filing_states: {
            first: { filing_id: "first", label: "2025", status: "validated", fiscal_period: "FY", elapsed_seconds: 10 },
            second: { filing_id: "second", label: "2024", status: "validated", fiscal_period: "FY", elapsed_seconds: 8 },
            cloud: { filing_id: "cloud", label: "2022", status: "cloud-processing", elapsed_seconds: 8,
              last_activity_at: "2026-10-26T10:00:00+08:00" },
            third: { filing_id: "third", label: "2023", status: "local-validating", waiting_type: "local_validation",
              fiscal_period: "FY", elapsed_seconds: 0, last_activity_at: "2026-10-26T10:10:00+08:00" },
            fourth: { filing_id: "fourth", label: "2022 annual", status: "queued", fiscal_period: "FY", elapsed_seconds: 0 },
          },
        }}
        labels={{ cancel: "Cancel", cancelling: "Stopping", agents: "Agents", running: "Running", retrying: "Retrying", queued: "Queued", completed: "Done", cancelled: "Cancelled", failed: "Failed", unknown: "Waiting" }}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getAllByText("Processing complete")).toHaveLength(2);
    expect(screen.getByText("Cloud processing")).toBeInTheDocument();
    expect(screen.getByText("Queued")).toBeInTheDocument();
    expect(screen.getByLabelText("Estimated remaining")).toBeInTheDocument();
  });

  it("exposes filing error codes as keyboard-accessible diagnostics", () => {
    render(
      <ResearchProgress
        language="en"
        job={{
          job_id: "diagnostic",
          state: "running",
          message: "",
          percent: 31,
          run_id: null,
          stage: "filing-validation",
          filing_states: {
            blocked: { filing_id: "blocked", label: "2025", status: "blocked", error_code: "FILING_VALIDATION_BLOCKED", elapsed_seconds: 12 },
          },
        }}
        labels={{ cancel: "Cancel", cancelling: "Stopping", agents: "Agents", running: "Running", retrying: "Retrying", queued: "Queued", completed: "Done", cancelled: "Cancelled", failed: "Failed", unknown: "Waiting" }}
        onCancel={vi.fn()}
      />,
    );
    const diagnostic = screen.getByLabelText("Filing diagnostic: FILING_VALIDATION_BLOCKED");
    expect(diagnostic).toHaveAttribute("tabindex", "0");
    expect(diagnostic).toHaveAttribute("title", expect.stringContaining("Open financial diagnostics"));
  });

  it("does not estimate remaining while cloud approval is waiting", () => {
    render(
      <ResearchProgress
        language="en"
        job={{
          job_id: "approval-eta",
          state: "running",
          message: "",
          percent: 42,
          run_id: null,
          stage: "vision-approval",
          stage_current: 2,
          stage_total: 5,
          elapsed_seconds: 30,
          vision_approval_pending: true,
        }}
        labels={{ cancel: "Cancel", cancelling: "Stopping", agents: "Agents", running: "Running", retrying: "Retrying", queued: "Queued", completed: "Done", cancelled: "Cancelled", failed: "Failed", unknown: "Waiting" }}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.queryByLabelText("Estimated remaining")).not.toBeInTheDocument();
  });

  it("replaces a divergent filing ETA with a stalled deep-parsing explanation", () => {
    render(
      <ResearchProgress
        language="en"
        job={{
          job_id: "stalled-filing",
          state: "running",
          message: "",
          percent: 47,
          run_id: null,
          stage: "filing-parse",
          stage_current: 1,
          stage_total: 10,
          elapsed_seconds: 765,
          stage_elapsed_seconds: 765,
        }}
        labels={{ cancel: "Cancel", cancelling: "Stopping", agents: "Agents", running: "Running", retrying: "Retrying", queued: "Queued", completed: "Done", cancelled: "Cancelled", failed: "Failed", unknown: "Waiting" }}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.queryByLabelText("Estimated remaining")).not.toBeInTheDocument();
    expect(screen.getByText(/Deep parsing is still active/)).toBeInTheDocument();
  });

  it("defaults to preparation, switches once on research activity, and preserves a later preparation selection", () => {
    const onCancel = vi.fn();
    const labels = { cancel: "Cancel research", cancelling: "Stopping…", agents: "Agents", running: "Running", retrying: "Retrying",
      queued: "Queued", completed: "Completed", cancelled: "Cancelled", failed: "Failed", unknown: "Waiting" };
    const initialJob = { job_id: "tabs", state: "running" as const, message: "Preparing", percent: 38, run_id: null,
      stage: "filing-validation", filing_states: { report: { filing_id: "report", label: "2025 report", status: "cloud-processing" as const, elapsed_seconds: 20 } } };
    const view = render(<ResearchProgress language="en" labels={labels} job={initialJob} onCancel={onCancel} />);
    const preparationTab = screen.getByRole("tab", { name: "Preparation" });
    expect(preparationTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("2025 report")).toBeInTheDocument();

    const researchJob = { ...initialJob, stage: "parallel-agents", agent_states: { "financial-analyst": "running" },
      model_calls: [{ request_id: "req", attempt_id: "one", generation: 1, stage_id: "financial-analyst", state: "running", elapsed_seconds: 12, model_id: "provider/model" }] };
    view.rerender(<ResearchProgress language="en" labels={labels} job={researchJob} onCancel={onCancel} />);
    expect(screen.getByRole("tab", { name: "Model research" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("provider/model")).toBeInTheDocument();

    fireEvent.click(preparationTab);
    expect(preparationTab).toHaveAttribute("aria-selected", "true");
    view.rerender(<ResearchProgress language="en" labels={labels} job={{ ...researchJob,
      elapsed_seconds: 19, model_calls: [{ ...researchJob.model_calls[0], state: "reasoning", elapsed_seconds: 19 }] }} onCancel={onCancel} />);
    expect(preparationTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("2025 report")).toBeInTheDocument();
    expect(screen.queryByText("Reasoning activity received")).not.toBeInTheDocument();
  });

  it("keeps research cancellation and per-call cancellation as distinct actions", () => {
    const onCancel = vi.fn();
    const onCancelCall = vi.fn();
    render(<ResearchProgress language="en" job={{ job_id: "cancel-actions", state: "running", message: "Working", percent: 50, run_id: null,
      stage: "parallel-agents", agent_states: { "financial-analyst": "running" }, model_calls: [{
        request_id: "req-current", attempt_id: "attempt-current", generation: 1, stage_id: "financial-analyst", state: "reasoning", elapsed_seconds: 24,
      }] }} labels={{ cancel: "Cancel research", cancelling: "Stopping…", agents: "Agents", running: "Running", retrying: "Retrying", queued: "Queued", completed: "Done", cancelled: "Cancelled", failed: "Failed", unknown: "Waiting" }}
      onCancel={onCancel} onCancelCall={onCancelCall} />);

    fireEvent.click(screen.getByRole("button", { name: "Stop waiting for this call" }));
    expect(onCancelCall).toHaveBeenCalledWith("req-current", "attempt-current");
    expect(onCancel).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel research" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onCancelCall).toHaveBeenCalledTimes(1);
  });

  it("passes the durable run identity into lazy call-history paging", async () => {
    const loadCallHistory = vi.fn(async () => ({ revision: "rev-1", calls: [], next_cursor: null }));
    render(<ResearchProgress language="en" job={{ job_id: "paging", state: "running", message: "Working", percent: 40, run_id: "run-1",
      stage: "parallel-agents", model_calls: [{ request_id: "req", attempt_id: "attempt", generation: 1,
        stage_id: "financial-analyst", state: "saved", elapsed_seconds: 1 }] }}
      labels={{ cancel: "Cancel research", cancelling: "Stopping…", agents: "Agents", running: "Running", retrying: "Retrying", queued: "Queued", completed: "Done", cancelled: "Cancelled", failed: "Failed", unknown: "Waiting" }}
      onCancel={vi.fn()} loadCallHistory={loadCallHistory} />);

    expect(loadCallHistory).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText(/Usage and call history/));
    await waitFor(() => expect(loadCallHistory).toHaveBeenCalledWith("run-1", "financial-analyst", undefined, 10));
  });

  it("shows optional per-file availability and provenance without calling every document verified", () => {
    render(<ResearchProgress language="en" job={{ job_id: "file-details", state: "running", message: "Reading", percent: 40, run_id: null,
      stage: "filing-validation", filing_states: {
        partial: { filing_id: "partial", label: "Annual filing", status: "validated", processing_state: "completed", availability: "partial",
          roles: ["target", "comparator"], candidate_count: 12, admitted_count: 8, issue_codes: ["MISSING_CASH_FLOW"], source_hash: "abcdef1234567890", elapsed_seconds: 31,
          page_count: 80, native_text_page_count: 72, indexed_page_count: 55, completed_page_count: 31, candidate_page_count: 55, completed_window_count: 4, window_total: 5,
          parse_elapsed_seconds: 18.4, attempt_count: 2, cache_reused: false, recognized_statements: ["balance_sheet"], extracted_pages: [2, 11],
          failure_stage: "parse", root_error_code: "FILING_PARSE_INCOMPLETE" },
        complete: { filing_id: "complete", label: "Complete filing", status: "validated", processing_state: "completed", availability: "complete", elapsed_seconds: 20 },
        none: { filing_id: "none", label: "No usable tables", status: "failed", processing_state: "failed", availability: "none", elapsed_seconds: 9 },
        history: { filing_id: "history", label: "Prior-year filing", status: "historical", elapsed_seconds: 18 },
        notApplicable: { filing_id: "na", label: "Quarterly report", status: "not-applicable", elapsed_seconds: 4 },
        action: { filing_id: "action", label: "Needs follow-up", status: "needs-action", elapsed_seconds: 3 },
      } }} onCancel={vi.fn()} />);
    expect(screen.getByText("Partially usable")).toBeInTheDocument();
    expect(screen.getByText("Usable content available")).toBeInTheDocument();
    expect(screen.getByText("No usable content")).toBeInTheDocument();
    expect(screen.getByText(/Roles: Target · Comparator/)).toBeInTheDocument();
    expect(screen.getByText(/Candidates: 12 · 8 admitted/)).toBeInTheDocument();
    expect(screen.getByText("Candidate pages complete: 31 / 55")).toBeInTheDocument();
    expect(screen.getByText("MISSING_CASH_FLOW")).toBeInTheDocument();
    expect(screen.getByText("Source hash: abcdef123456")).toBeInTheDocument();
    expect(screen.getByText("Historical/comparative material processed")).toBeInTheDocument();
    expect(screen.getByText("Not applicable")).toBeInTheDocument();
    expect(screen.getByText("Needs action")).toBeInTheDocument();
    expect(screen.queryByText("Validated")).not.toBeInTheDocument();

    const parsingDetails = document.querySelector(".filing-progress-details") as HTMLDetailsElement;
    expect(parsingDetails).not.toHaveAttribute("open");
    fireEvent.click(parsingDetails.querySelector("summary") as HTMLElement);
    expect(parsingDetails).toHaveTextContent("Total pages80");
    expect(parsingDetails).toHaveTextContent("Native-text pages72");
    expect(parsingDetails).toHaveTextContent("Indexed pages55");
    expect(parsingDetails).toHaveTextContent("Candidate pages complete31 / 55");
    expect(parsingDetails).toHaveTextContent("Legacy windows complete4 / 5");
    expect(parsingDetails).toHaveTextContent("Parse time00:18");
    expect(parsingDetails).toHaveTextContent("Attempts2");
    expect(parsingDetails).toHaveTextContent("Cached result reusedNo");
    expect(parsingDetails).toHaveTextContent("Recognized statementsbalance_sheet");
    expect(parsingDetails).toHaveTextContent("Pages with extracted facts2, 11");
    expect(parsingDetails).toHaveTextContent("Root error codeFILING_PARSE_INCOMPLETE");
  });

  it.each([
    ["en", "Roles: Target · Comparator · Historical"],
    ["zh-CN", "角色: 目标公司 · 对照公司 · 历史资料"],
    ["zh-Hant", "角色: 目標公司 · 比較公司 · 歷史資料"],
  ])("localizes filing role identifiers in %s", (language, expectedRoles) => {
    render(<ResearchProgress language={language} job={{ job_id: `roles-${language}`, state: "running", message: "Reading", percent: 20, run_id: null,
      filing_states: { annual: { filing_id: "annual", label: "Annual filing", status: "validated", roles: ["target", "comparator", "historical"], elapsed_seconds: 2 } } }} onCancel={vi.fn()} />);
    expect(screen.getByText(expectedRoles)).toBeInTheDocument();
  });
});
