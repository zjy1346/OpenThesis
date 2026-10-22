import { act, render, screen } from "@testing-library/react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { getBackendInitializationStatus } from "../backend";
import { CoreStartup } from "./CoreStartup";

vi.mock("../backend", () => ({ getBackendInitializationStatus: vi.fn() }));

describe("CoreStartup", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it.each([
    ["en", "Backing up local data", "Backup pages: 4 / 12"],
    ["zh-CN", "备份本地数据", "备份页数: 4 / 12"],
    ["zh-Hant", "備份本地資料", "備份頁數: 4 / 12"],
  ])("shows the actual backup stage and page progress in %s", async (language, stage, progress) => {
    vi.mocked(getBackendInitializationStatus).mockResolvedValue({ phase: "starting", elapsed_seconds: 6, deadline_seconds: 240,
      safe_failure_code: null, stage: "backing_up", completed_units: 4, total_units: 12 });
    render(<CoreStartup language={language} />);

    await act(async () => { await vi.advanceTimersByTimeAsync(500); });

    expect(screen.getByText(stage)).toBeInTheDocument();
    expect(screen.getByText(progress)).toBeInTheDocument();
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
  });

  it("does not invent backup progress when the unit counts are unavailable or invalid", async () => {
    vi.mocked(getBackendInitializationStatus).mockResolvedValue({ phase: "starting", elapsed_seconds: 6, deadline_seconds: 240,
      safe_failure_code: null, stage: "backing_up", completed_units: null, total_units: 0 });
    render(<CoreStartup language="en" />);

    await act(async () => { await vi.advanceTimersByTimeAsync(500); });

    expect(screen.getByText("Backing up local data")).toBeInTheDocument();
    expect(screen.queryByText(/Backup pages/)).not.toBeInTheDocument();
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
  });

  it("uses only known safe stage labels", async () => {
    vi.mocked(getBackendInitializationStatus).mockResolvedValue({ phase: "starting", elapsed_seconds: 6, deadline_seconds: 240,
      safe_failure_code: null, stage: "C:/private/path", completed_units: 3, total_units: 4 });
    render(<CoreStartup language="en" />);

    await act(async () => { await vi.advanceTimersByTimeAsync(500); });

    expect(screen.queryByText("C:/private/path")).not.toBeInTheDocument();
    expect(screen.queryByText(/Backup pages/)).not.toBeInTheDocument();
  });
});
