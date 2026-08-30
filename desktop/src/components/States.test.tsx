import { act, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ResearchProgress } from "./States";

describe("ResearchProgress", () => {
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
            first: { filing_id: "first", label: "2025", status: "validated", elapsed_seconds: 10 },
            second: { filing_id: "second", label: "2024", status: "cloud-processing", elapsed_seconds: 8 },
            third: { filing_id: "third", label: "2023", status: "queued", elapsed_seconds: 0 },
          },
        }}
        labels={{ cancel: "Cancel", cancelling: "Stopping", agents: "Agents", running: "Running", retrying: "Retrying", queued: "Queued", completed: "Done", cancelled: "Cancelled", failed: "Failed", unknown: "Waiting" }}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getByText("Validated")).toBeInTheDocument();
    expect(screen.getByText("Cloud processing")).toBeInTheDocument();
    expect(screen.getByText("Queued")).toBeInTheDocument();
    expect(screen.getByLabelText("Estimated remaining")).toBeInTheDocument();
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
});
