import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  archiveResearchTask,
  cancelModelCall,
  cancelResearch,
  continueResearchQueue,
  decideVisionUpload,
  getResearchStatus,
  listResearchTasks,
  resumeResearchTask,
} from "../backend";
import type { ResearchJob, ResearchTaskList, ResearchTaskSummary } from "../types";
import { useResearchTasks } from "./useResearchTasks";

vi.mock("../backend", () => ({
  archiveResearchTask: vi.fn(),
  cancelModelCall: vi.fn(),
  cancelResearch: vi.fn(),
  continueResearchQueue: vi.fn(),
  extendResearchBudget: vi.fn(),
  decideVisionUpload: vi.fn(),
  getResearchStatus: vi.fn(),
  listResearchTasks: vi.fn(),
  resumeResearchTask: vi.fn(),
}));

const summary = (jobId: string, state: ResearchTaskSummary["state"], revision = 1): ResearchTaskSummary => ({
  job_id: jobId,
  run_id: "run-" + jobId,
  state,
  revision,
  operation_type: "research",
  company: { name: "Company " + jobId, ticker: jobId.toUpperCase() },
  model: "model-a",
  stage: state === "queued" ? "queued" : "synthesis",
  message: jobId + " " + state,
  elapsed_seconds: 12,
  queue_position: state === "queued" ? 1 : undefined,
  ahead_count: state === "queued" ? 0 : undefined,
  created_at: "2026-10-28T00:00:00Z",
});

const taskList = (
  items: ResearchTaskSummary[],
  revision = 1,
  overrides: Partial<ResearchTaskList> = {},
): ResearchTaskList => ({
  items,
  revision,
  not_modified: false,
  counts: { running: 0, queued: 0, waiting_input: 0, interrupted: 0, cancelling: 0 },
  queue_paused: false,
  has_more: false,
  next_offset: null,
  ...overrides,
});

const job = (jobId: string, state: ResearchJob["state"], revision = 1): ResearchJob => ({
  job_id: jobId,
  run_id: "run-" + jobId,
  state,
  operation_type: "research",
  company: { name: "Company " + jobId, ticker: jobId.toUpperCase() },
  model: "model-a",
  stage: state === "queued" ? "queued" : "synthesis",
  message: jobId + " " + state,
  percent: 25,
  revision,
  created_at: "2026-10-28T00:00:00Z",
});

describe("useResearchTasks", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    vi.mocked(listResearchTasks).mockResolvedValue(taskList([]));
    vi.mocked(getResearchStatus).mockImplementation(async (jobId) => job(jobId, "queued"));
    vi.mocked(cancelResearch).mockImplementation(async (jobId) => job(jobId, "cancelled", 2));
    vi.mocked(cancelModelCall).mockImplementation(async (jobId) => job(jobId, "running", 2));
    vi.mocked(decideVisionUpload).mockImplementation(async (jobId) => job(jobId, "running", 2));
    vi.mocked(resumeResearchTask).mockImplementation(async (jobId) => job(jobId, "queued", 2));
    vi.mocked(archiveResearchTask).mockResolvedValue({ job_id: "A", archived: true });
    vi.mocked(continueResearchQueue).mockResolvedValue(taskList([]));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("does not poll when disabled and merges accepted jobs without replacing newer revisions", () => {
    const { result } = renderHook(() => useResearchTasks(false));
    expect(listResearchTasks).not.toHaveBeenCalled();

    act(() => {
      result.current.acceptJob(job("A", "running", 3));
      result.current.acceptJob(job("A", "queued", 2));
    });

    expect(result.current.items).toHaveLength(1);
    expect(result.current.items[0]).toMatchObject({ job_id: "A", state: "running", revision: 3 });
    expect(result.current.selectedTaskId).toBe("A");
  });

  it("does not let the first list response consume a terminal notification created by acceptJob", async () => {
    const finishLists: Array<(value: ResearchTaskList) => void> = [];
    vi.mocked(listResearchTasks).mockImplementation(() => new Promise((resolve) => { finishLists.push(resolve); }));
    const { result } = renderHook(() => useResearchTasks(true));
    act(() => result.current.acceptJob(job("A", "completed", 4)));
    expect(result.current.notifications).toHaveLength(1);

    await act(async () => {
      for (const finish of finishLists) finish(taskList([summary("A", "completed", 4)], 4));
    });

    expect(result.current.notifications).toHaveLength(1);
    expect(result.current.notifications[0]?.id).toBe("A:4");
  });

  it("merges pages by id and ignores older summary revisions on refresh", async () => {
    vi.mocked(listResearchTasks)
      .mockResolvedValueOnce(taskList([summary("A", "running", 2)], 2, { has_more: true, next_offset: 50 }))
      .mockResolvedValueOnce(taskList([summary("B", "queued", 1)], 2))
      .mockResolvedValueOnce(taskList([summary("A", "failed", 1), summary("C", "interrupted", 1)], 3));
    const { result } = renderHook(() => useResearchTasks(true));
    await waitFor(() => expect(result.current.items.map((item) => item.job_id)).toEqual(["A"]));

    await act(async () => { await result.current.loadMore(); });
    expect(result.current.items.map((item) => item.job_id).sort()).toEqual(["A", "B"]);

    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.items.some((item) => item.job_id === "C")).toBe(true));
    expect(result.current.items.find((item) => item.job_id === "A")).toMatchObject({ state: "running", revision: 2 });
    expect(result.current.items.map((item) => item.job_id).sort()).toEqual(["A", "B", "C"]);
  });

  it("keeps loaded pagination offsets across first-page polling and does not revision-gate later pages", async () => {
    vi.useFakeTimers();
    vi.mocked(listResearchTasks)
      .mockResolvedValueOnce(taskList([summary("A", "running", 1)], 1, { has_more: true, next_offset: 50 }))
      .mockResolvedValueOnce(taskList([summary("B", "queued", 1)], 1, { has_more: true, next_offset: 100 }))
      .mockResolvedValueOnce(taskList([summary("A", "running", 2)], 2, { has_more: true, next_offset: 50 }))
      .mockResolvedValueOnce(taskList([summary("C", "queued", 1)], 2));
    const { result } = renderHook(() => useResearchTasks(true));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(result.current.items.map((item) => item.job_id)).toEqual(["A"]);

    await act(async () => { await result.current.loadMore(); });
    expect(vi.mocked(listResearchTasks).mock.calls[1][0]).toEqual({ limit: 50, offset: 50 });
    expect(result.current.items.map((item) => item.job_id).sort()).toEqual(["A", "B"]);

    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    expect(result.current.items.find((item) => item.job_id === "A")?.revision).toBe(2);
    await act(async () => { await result.current.loadMore(); });

    expect(vi.mocked(listResearchTasks).mock.calls[3][0]).toEqual({ limit: 50, offset: 100 });
    expect(result.current.items.map((item) => item.job_id).sort()).toEqual(["A", "B", "C"]);
  });

  it("ignores a first-page response from before refresh", async () => {
    let finishOld!: (value: ResearchTaskList) => void;
    vi.mocked(listResearchTasks)
      .mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve; }))
      .mockResolvedValueOnce(taskList([summary("C", "queued", 3)], 3));
    const { result } = renderHook(() => useResearchTasks(true));

    await waitFor(() => expect(listResearchTasks).toHaveBeenCalledTimes(1));
    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.items.map((item) => item.job_id)).toEqual(["C"]));
    await act(async () => { finishOld(taskList([summary("A", "running", 1)], 1)); });

    expect(result.current.items.map((item) => item.job_id)).toEqual(["C"]);
    expect(result.current.items[0]?.revision).toBe(3);
  });

  it("guards late detail responses by selection and task identity", async () => {
    vi.mocked(listResearchTasks).mockResolvedValue(taskList([
      summary("A", "running"), summary("B", "running"),
    ]));
    let finishA!: (value: ResearchJob) => void;
    vi.mocked(getResearchStatus).mockImplementation((jobId) => jobId === "A"
      ? new Promise((resolve) => { finishA = resolve; })
      : Promise.resolve(job("B", "running", 1)));
    const { result } = renderHook(() => useResearchTasks(true));
    await waitFor(() => expect(result.current.items).toHaveLength(2));

    act(() => result.current.setSelectedTaskId("A"));
    act(() => result.current.setSelectedTaskId("B"));
    await waitFor(() => expect(result.current.detail?.job_id).toBe("B"));
    await act(async () => { finishA(job("A", "running", 8)); });

    expect(result.current.selectedTaskId).toBe("B");
    expect(result.current.detail?.job_id).toBe("B");
  });

  it("keeps the loaded detail when the same task is selected again", async () => {
    vi.mocked(listResearchTasks).mockResolvedValue(taskList([summary("A", "completed", 2)]));
    vi.mocked(getResearchStatus).mockResolvedValue(job("A", "completed", 2));
    const { result } = renderHook(() => useResearchTasks(true));
    await waitFor(() => expect(result.current.items).toHaveLength(1));
    act(() => result.current.selectTask("A"));
    await waitFor(() => expect(result.current.detail?.job_id).toBe("A"));
    const loaded = result.current.detail;
    act(() => result.current.selectTask("A"));
    expect(result.current.detail).toBe(loaded);
    expect(getResearchStatus).toHaveBeenCalledTimes(1);
  });

  it("polls only the selected active detail and stops polling after selection changes", async () => {
    vi.mocked(listResearchTasks).mockResolvedValue(taskList([
      summary("A", "running"), summary("B", "running"),
    ]));
    const { result } = renderHook(() => useResearchTasks(true));
    await waitFor(() => expect(result.current.items).toHaveLength(2));
    vi.useFakeTimers();
    vi.mocked(getResearchStatus).mockImplementation(async (jobId) => job(jobId, "running"));

    act(() => result.current.setSelectedTaskId("A"));
    await act(async () => { await Promise.resolve(); });
    expect(getResearchStatus).toHaveBeenCalledTimes(1);
    expect(getResearchStatus).toHaveBeenLastCalledWith("A");

    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    expect(getResearchStatus).toHaveBeenCalledTimes(2);
    expect(getResearchStatus).toHaveBeenLastCalledWith("A");

    act(() => result.current.setSelectedTaskId("B"));
    await act(async () => { await Promise.resolve(); });
    expect(getResearchStatus).toHaveBeenCalledTimes(3);
    expect(getResearchStatus).toHaveBeenLastCalledWith("B");
  });

  it("retries a transient detail-status failure with backoff and stops after a terminal response", async () => {
    vi.useFakeTimers();
    vi.mocked(listResearchTasks).mockResolvedValue(taskList([summary("A", "running")]));
    vi.mocked(getResearchStatus)
      .mockRejectedValueOnce(new Error("temporary status outage"))
      .mockResolvedValueOnce(job("A", "running", 2))
      .mockResolvedValueOnce(job("A", "completed", 3));
    const { result } = renderHook(() => useResearchTasks(true));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => result.current.setSelectedTaskId("A"));

    await act(async () => { await Promise.resolve(); });
    expect(getResearchStatus).toHaveBeenCalledTimes(1);
    expect(result.current.detailError).toBe("temporary status outage");
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    expect(getResearchStatus).toHaveBeenCalledTimes(2);
    expect(result.current.detailError).toBe("");
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    expect(getResearchStatus).toHaveBeenCalledTimes(3);
    expect(result.current.detail?.state).toBe("completed");
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(getResearchStatus).toHaveBeenCalledTimes(3);
  });

  it("does not restart or overlap a slow detail request on every list revision", async () => {
    vi.useFakeTimers();
    let revision = 1;
    vi.mocked(listResearchTasks).mockImplementation(async () => taskList([summary("A", "running", revision)], revision++));
    let finish!: (value: ResearchJob) => void;
    vi.mocked(getResearchStatus).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }))
      .mockResolvedValue(job("A", "running", 100));
    const { result, unmount } = renderHook(() => useResearchTasks(true));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => result.current.selectTask("A"));
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    expect(getResearchStatus).toHaveBeenCalledTimes(1);
    await act(async () => { finish(job("A", "running", 100)); });
    expect(result.current.detail?.revision).toBe(100);
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    expect(getResearchStatus).toHaveBeenCalledTimes(2);
    unmount();
  });

  it("notifies once on a newly observed terminal revision and not for historical terminal jobs", async () => {
    vi.useFakeTimers();
    vi.mocked(listResearchTasks)
      .mockResolvedValueOnce(taskList([summary("A", "queued", 1)], 1))
      .mockResolvedValueOnce(taskList([summary("A", "completed", 2)], 2))
      .mockResolvedValue(taskList([summary("A", "completed", 2)], 2));
    const { result, unmount } = renderHook(() => useResearchTasks(true));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(result.current.items[0]?.state).toBe("queued");

    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    expect(result.current.notifications).toHaveLength(1);
    expect(result.current.notifications[0]).toMatchObject({ job_id: "A", state: "completed", revision: 2 });
    act(() => result.current.dismissNotification("A"));
    expect(result.current.notifications).toHaveLength(0);
    unmount();

    vi.useRealTimers();
    vi.mocked(listResearchTasks).mockResolvedValue(taskList([summary("A", "completed", 2)], 2));
    const reloaded = renderHook(() => useResearchTasks(true));
    await waitFor(() => expect(reloaded.result.current.items[0]?.state).toBe("completed"));
    expect(reloaded.result.current.notifications).toHaveLength(0);
  });

  it("keeps cancel, model cancellation, vision approval, and resume scoped to explicit task ids and revisions", async () => {
    const { result } = renderHook(() => useResearchTasks(false));
    act(() => {
      result.current.acceptJob(job("A", "running", 1));
      result.current.acceptJob(job("B", "queued", 1));
    });
    await act(async () => {
      await result.current.cancel("B");
      await result.current.cancelModel("C", "request-1", "attempt-2");
      await result.current.decideVisionUpload("D", 9, "plan-4", true);
      await result.current.resume("E", 11);
    });

    expect(cancelResearch).toHaveBeenCalledWith("B");
    expect(cancelModelCall).toHaveBeenCalledWith("C", "request-1", "attempt-2");
    expect(decideVisionUpload).toHaveBeenCalledWith("D", true, 9, "plan-4");
    expect(resumeResearchTask).toHaveBeenCalledWith("E", 11, undefined, false);
    expect(result.current.items.find((item) => item.job_id === "A")?.state).toBe("running");
    expect(result.current.items.find((item) => item.job_id === "B")?.state).toBe("cancelled");
  });
});
