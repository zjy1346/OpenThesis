import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ResearchProgress } from "./States";

describe("ResearchProgress", () => {
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
        onCancel={vi.fn()}
      />,
    );

    expect(screen.getByText("Stopping unfinished agents…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stopping…" })).toBeDisabled();
    expect(screen.getByText("financial-analyst")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.getByText("Queued")).toBeInTheDocument();
  });
});
