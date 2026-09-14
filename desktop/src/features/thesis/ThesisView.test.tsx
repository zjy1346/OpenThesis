import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { getThesis, listTheses, saveThesis } from "../../backend";
import { ThesisView } from "./ThesisView";

vi.mock("../../backend", () => ({
  getThesis: vi.fn(),
  listTheses: vi.fn(),
  saveThesis: vi.fn(),
}));

const copy = {
  thesesTitle: "Investment thesis versions",
  thesesBody: "Every save creates an append-only version.",
  noTheses: "No theses.",
  thesisJson: "Investment thesis JSON",
  saveNewVersion: "Save as new version",
  saving: "Saving…",
  saved: "Saved.",
  invalidJson: "Invalid JSON.",
  loading: "Loading…",
};

const previous = {
  thesis_version_id: "0001:v1",
  company_cik: "0001",
  run_id: "run-1",
  version: 1,
  ticker: "TEST",
  name: "Test Company",
  created_at: "2026-09-10T00:00:00Z",
  created_by: "model",
  content: {
    thesis: "Old thesis",
    key_assumptions: ["old assumption", { legacy: true }],
    supporting_evidence_ids: ["source-old", { legacy: "keep" }],
    custom_legacy: { keep: ["all", 42] },
  },
};

const latest = {
  ...previous,
  thesis_version_id: "0001:v2",
  version: 2,
  created_at: "2026-09-11T00:00:00Z",
  content: {
    ...previous.content,
    thesis: "New thesis",
    key_assumptions: ["new assumption", { legacy: true }],
    supporting_evidence_ids: ["source-old", "source-new", { legacy: "keep" }],
    change_reason: "Updated after earnings",
  },
};

describe("ThesisView investor workbench", () => {
  beforeEach(() => {
    vi.mocked(listTheses).mockResolvedValue([previous]);
    vi.mocked(getThesis).mockResolvedValue(previous);
    vi.mocked(saveThesis).mockResolvedValue(latest);
  });

  it("edits structured fields, requires a reason, saves and reloads without losing legacy data", async () => {
    render(<ThesisView copy={copy} />);
    expect(await screen.findByLabelText("Core thesis")).toHaveValue("Old thesis");
    fireEvent.change(screen.getByLabelText("Core thesis"), { target: { value: "New thesis" } });
    fireEvent.change(screen.getByLabelText("Reason for change"), { target: { value: "Updated after earnings" } });
    fireEvent.click(screen.getByRole("button", { name: "Save as new version" }));
    await waitFor(() => expect(saveThesis).toHaveBeenCalledWith("0001", expect.objectContaining({
      thesis: "New thesis",
      change_reason: "Updated after earnings",
      custom_legacy: { keep: ["all", 42] },
      key_assumptions: ["old assumption", { legacy: true }],
      supporting_evidence_ids: ["source-old", { legacy: "keep" }],
    }), previous.thesis_version_id));
    expect(await screen.findByText(/Saved/)).toBeInTheDocument();
    expect(screen.getAllByText(/Added|新增/).length).toBeGreaterThan(0);
  });

  it("shows same-company field and evidence diffs without inventing a reason", async () => {
    vi.mocked(listTheses).mockResolvedValue([latest, previous]);
    vi.mocked(getThesis).mockResolvedValue(latest);
    render(<ThesisView copy={copy} />);
    expect(await screen.findByDisplayValue(/source-new/)).toBeInTheDocument();
    expect(screen.getAllByText("Updated after earnings").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Changed|变更/).length).toBeGreaterThan(0);
  });

  it("distinguishes fetch, validation, and save errors", async () => {
    vi.mocked(listTheses).mockRejectedValueOnce(new Error("network"));
    const { unmount } = render(<ThesisView copy={copy} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not load|加载失败/);
    unmount();

    vi.mocked(listTheses).mockResolvedValueOnce([previous]);
    vi.mocked(getThesis).mockResolvedValueOnce(previous);
    render(<ThesisView copy={copy} />);
    await screen.findByLabelText("Core thesis");
    fireEvent.change(screen.getByLabelText("Core thesis"), { target: { value: "Edited" } });
    fireEvent.click(screen.getByRole("button", { name: "Save as new version" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/reason|required|原因/);

    fireEvent.change(screen.getByLabelText("Reason for change"), { target: { value: "Because" } });
    vi.mocked(saveThesis).mockRejectedValueOnce(new Error("write"));
    fireEvent.click(screen.getByRole("button", { name: "Save as new version" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not save|保存失败/);
  });
});
