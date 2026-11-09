import { act, renderHook, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { listResearchTasks } from "../backend";
import type { ResearchTaskList, ResearchTaskSummary } from "../types";
import { useResearchTaskCatalog } from "./useResearchTaskCatalog";

vi.mock("../backend", () => ({ listResearchTasks: vi.fn() }));

const row = (id: string): ResearchTaskSummary => ({ job_id: id, run_id: `run-${id}`, state: "queued",
  company: { name: id, ticker: id }, revision: 1, stage: "queued", message: "", model: "fixture",
  operation_type: "research", elapsed_seconds: 0, created_at: "2026-11-08T00:00:00Z" });
const page = (revision: number, items: ResearchTaskSummary[], next_offset: number | null = null): ResearchTaskList => ({
  revision, items, next_offset, has_more: next_offset !== null, not_modified: false, queue_paused: false,
  counts: { running: 1, queued: 51, waiting_input: 0, interrupted: 0, cancelling: 0 },
});

it("updates the viewed later page even if the first page identities do not change", async () => {
  let revision = 1;
  const head = Array.from({ length: 50 }, (_, index) => row(`head-${index}`));
  const observe = vi.fn();
  vi.mocked(listResearchTasks).mockImplementation(async ({ offset } = {}) => offset === 50
    ? page(revision, [row(revision === 1 ? "old-later-row" : "new-later-row")]) : page(revision, head, 50));
  const { result, rerender } = renderHook(({ background }) => useResearchTaskCatalog(true, background, observe),
    { initialProps: { background: page(1, head, 50) } });
  await waitFor(() => expect(result.current.items[0]?.job_id).toBe("head-0"));
  act(() => result.current.nextPage());
  await waitFor(() => expect(result.current.items.map(({ job_id }) => job_id)).toContain("old-later-row"));

  vi.mocked(listResearchTasks).mockClear();
  revision = 2;
  rerender({ background: page(2, head, 50) });
  await waitFor(() => expect(result.current.items.map(({ job_id }) => job_id)).toContain("new-later-row"));
  expect(result.current.items.map(({ job_id }) => job_id)).not.toContain("old-later-row");
  expect(result.current.pageNumber).toBe(2);
  expect(listResearchTasks).toHaveBeenCalledExactlyOnceWith({ limit: 50, offset: 50,
    since_revision: 1, states: ["running", "queued", "cancelling"] });
});

it("ignores an old page reply after returning to the first page", async () => {
  const head = [row("current")];
  let late!: (response: ResearchTaskList) => void;
  const observe = vi.fn();
  vi.mocked(listResearchTasks).mockImplementation(({ offset } = {}) => offset === 50
    ? new Promise((resolve) => { late = resolve; }) : Promise.resolve(page(4, head, 50)));
  const { result } = renderHook(() => useResearchTaskCatalog(true, null, observe));
  await waitFor(() => expect(result.current.hasMore).toBe(true));
  act(() => result.current.nextPage());
  await waitFor(() => expect(late).toBeTypeOf("function"));
  expect(result.current.hasPrevious).toBe(true);
  act(() => result.current.previousPage());
  await waitFor(() => expect(result.current.items).toEqual(head));
  await act(async () => late(page(4, [row("stale-page")])));
  expect(result.current.items).toEqual(head);
  expect(result.current.pageNumber).toBe(1);
});

it("coalesces background revisions while the visible page read is slow", async () => {
  const head = [row("current")];
  let settle!: (response: ResearchTaskList) => void;
  vi.mocked(listResearchTasks).mockReset()
    .mockResolvedValueOnce(page(1, head))
    .mockImplementationOnce(() => new Promise((resolve) => { settle = resolve; }))
    .mockResolvedValue(page(3, [row("after-coalescing")]));
  const observe = vi.fn();
  const { result, rerender } = renderHook(({ background }) => useResearchTaskCatalog(true, background, observe),
    { initialProps: { background: page(1, head) } });
  await waitFor(() => expect(result.current.items).toEqual(head));
  rerender({ background: page(2, head) });
  await waitFor(() => expect(settle).toBeTypeOf("function"));
  rerender({ background: page(3, head) });
  expect(listResearchTasks).toHaveBeenCalledTimes(2);
  await act(async () => settle(page(2, head)));
  await waitFor(() => expect(result.current.items[0]?.job_id).toBe("after-coalescing"));
  expect(listResearchTasks).toHaveBeenCalledTimes(3);
  expect(listResearchTasks).toHaveBeenLastCalledWith({ limit: 50, offset: 0, since_revision: 2,
    states: ["running", "queued", "cancelling"] });
});
