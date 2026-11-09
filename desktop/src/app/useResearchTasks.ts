import { useCallback, useEffect, useRef, useState } from "react";

import {
  archiveResearchTask,
  cancelModelCall,
  cancelResearch,
  continueResearchQueue,
  decideVisionUpload,
  extendResearchBudget,
  getResearchStatus,
  listResearchTasks,
  resumeResearchTask,
} from "../backend";
import type {
  ModelReference,
  ResearchJob,
  ResearchTaskCounts,
  ResearchTaskList,
  ResearchTaskState,
  ResearchTaskSummary,
} from "../types";
import { useResearchTaskCatalog } from "./useResearchTaskCatalog";
import { TASK_FILTER_STATES } from "../features/tasks/taskPresentation";

const PAGE_SIZE = 50;
const NOTIFICATION_STORAGE_KEY = "openthesis.research-task-notifications.v1";
const TERMINAL_STATES = new Set<ResearchTaskState>(["completed", "failed", "cancelled"]);
const POLLED_STATES = new Set<ResearchTaskState>(["running", "cancelling"]);

export type ResearchTaskNotification = Pick<
  ResearchTaskSummary,
  "job_id" | "run_id" | "state" | "revision" | "company" | "operation_type" | "message" | "result_access" | "research_complete"
> & { id: string };

type TaskListOptions = { limit: number; offset: number; since_revision?: number };

function emptyCounts(): ResearchTaskCounts {
  return { running: 0, queued: 0, waiting_input: 0, interrupted: 0, cancelling: 0 };
}

function summaryFromJob(job: ResearchJob, previous?: ResearchTaskSummary): ResearchTaskSummary {
  return {
    ...(previous ?? {} as ResearchTaskSummary),
    job_id: job.job_id,
    run_id: job.run_id ?? previous?.run_id ?? "",
    state: job.state,
    revision: job.revision ?? previous?.revision ?? 0,
    operation_type: job.operation_type ?? previous?.operation_type ?? "research",
    company: job.company ?? previous?.company ?? { name: "", ticker: "" },
    model: job.model ?? previous?.model ?? "",
    stage: job.stage ?? previous?.stage ?? "",
    message: job.message,
    elapsed_seconds: job.elapsed_seconds ?? previous?.elapsed_seconds ?? 0,
    ...(job.queue_position !== undefined ? { queue_position: job.queue_position } : {}),
    ...(job.ahead_count !== undefined ? { ahead_count: job.ahead_count } : {}),
    created_at: job.created_at ?? previous?.created_at ?? new Date(0).toISOString(),
    started_at: job.started_at ?? previous?.started_at ?? null,
    finished_at: job.finished_at ?? previous?.finished_at ?? null,
    ...(job.blocked_reason !== undefined ? { blocked_reason: job.blocked_reason } : {}),
    ...(job.action_required !== undefined ? { action_required: job.action_required } : {}),
    ...(job.research_complete !== undefined ? { research_complete: job.research_complete } : {}),
    ...(job.result_access !== undefined ? { result_access: job.result_access } : {}),
    execution_draining: job.execution_draining,
  };
}

function readNotificationKeys(): Set<string> {
  try {
    const saved = window.localStorage.getItem(NOTIFICATION_STORAGE_KEY);
    const parsed: unknown = saved ? JSON.parse(saved) : [];
    if (Array.isArray(parsed)) return new Set(parsed.filter((value): value is string => typeof value === "string"));
  } catch {
    // Storage may be disabled; the in-memory set still prevents duplicate notices this session.
  }
  return new Set();
}

function persistNotificationKeys(keys: Set<string>): void {
  try {
    window.localStorage.setItem(NOTIFICATION_STORAGE_KEY, JSON.stringify([...keys].slice(-500)));
  } catch {
    // Notifications remain usable for this session when persistent storage is unavailable.
  }
}

function notificationKey(task: Pick<ResearchTaskSummary, "job_id" | "revision">): string {
  return `${task.job_id}:${task.revision}`;
}

function mergeSummaries(current: ResearchTaskSummary[], incoming: ResearchTaskSummary[]): ResearchTaskSummary[] {
  const byId = new Map(current.map((item) => [item.job_id, item]));
  for (const item of incoming) {
    const previous = byId.get(item.job_id);
    if (!previous || item.revision >= previous.revision) byId.set(item.job_id, { ...previous, ...item });
  }
  return [...byId.values()];
}

function taskCountsForTransition(counts: ResearchTaskCounts, before: ResearchTaskState | undefined, after: ResearchTaskState): ResearchTaskCounts {
  const next = { ...counts };
  for (const state of ["running", "queued", "waiting_input", "interrupted", "cancelling"] as const) {
    if (before === state) next[state] = Math.max(0, next[state] - 1);
    if (after === state) next[state] += 1;
  }
  return next;
}

function normalizeOptions(options: TaskListOptions): TaskListOptions {
  return { ...options };
}

export function useResearchTasks(enabled: boolean, { catalogVisible = false, detailVisible = true, quickVisible = false } = {}) {
  const [items, setItems] = useState<ResearchTaskSummary[]>([]);
  const [counts, setCounts] = useState<ResearchTaskCounts>(emptyCounts);
  const [queuePaused, setQueuePaused] = useState(false);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState("");
  const [hasMore, setHasMore] = useState(false);
  const [selectedTaskId, setSelectedTaskIdState] = useState<string | null>(null);
  const [detail, setDetail] = useState<ResearchJob | null>(null);
  const [notifications, setNotifications] = useState<ResearchTaskNotification[]>([]);
  const [backgroundHead, setBackgroundHead] = useState<ResearchTaskList | null>(null);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [detailVersion, setDetailVersion] = useState(0);
  const [detailError, setDetailError] = useState("");
  const [commands, setCommands] = useState<Record<string, { pending: boolean; error: string }>>({});
  const pendingCommands = useRef(new Set<string>());
  const itemsRef = useRef(items);
  const countsRef = useRef(counts);
  const detailRef = useRef(detail);
  const revisionRef = useRef<number | undefined>(undefined);
  const nextOffsetRef = useRef<number | null>(null);
  const loadedThroughOffsetRef = useRef(0);
  const selectedTaskIdRef = useRef<string | null>(selectedTaskId);
  const selectionGenerationRef = useRef(0);
  const notificationKeysRef = useRef<Set<string> | null>(null);
  const initialListReceivedRef = useRef(false);
  const loadMoreInFlightRef = useRef(false);
  const listRequestGenerationRef = useRef(0);
  const detailRequestsRef = useRef(new Set<string>());
  const lastHead = useRef<ResearchTaskList | null>(null);
  const rowListRevisions = useRef(new Map<string, number>());

  itemsRef.current = items;
  countsRef.current = counts;
  detailRef.current = detail;
  if (notificationKeysRef.current === null) notificationKeysRef.current = readNotificationKeys();

  const setSelectedTaskId = useCallback((id: string | null) => {
    if (selectedTaskIdRef.current === id) return;
    selectionGenerationRef.current += 1;
    selectedTaskIdRef.current = id;
    detailRef.current = null;
    setSelectedTaskIdState(id);
    setDetail(null);
    setDetailError("");
  }, []);

  const notify = useCallback((task: ResearchTaskSummary) => {
    const key = notificationKey(task);
    const seen = notificationKeysRef.current!;
    const notice: ResearchTaskNotification = {
      id: key, job_id: task.job_id, run_id: task.run_id, state: task.state,
      revision: task.revision, company: task.company, operation_type: task.operation_type,
      message: task.message, result_access: task.result_access, research_complete: task.research_complete,
    };
    if (seen.has(key)) {
      // The durable list may confirm result access after a command response.
      // Enrich an existing notice, but never reopen one the user dismissed.
      if (notice.result_access) setNotifications((current) => current.some((item) => item.id === key && item.result_access !== notice.result_access)
        ? current.map((item) => item.id === key ? notice : item) : current);
      return;
    }
    seen.add(key);
    persistNotificationKeys(seen);
    setNotifications((current) => [...current, notice]);
  }, []);

  const observeRows = useCallback((rows: ResearchTaskSummary[], revision: number, notifyUpdates = true) => {
    if (!rows.length) return;
    const previousById = new Map(itemsRef.current.map((item) => [item.job_id, item]));
    const accepted = rows.filter((item) => {
      const previous = previousById.get(item.job_id);
      if (previous && item.revision < previous.revision) return false;
      if (previous && item.revision === previous.revision
          && revision < (rowListRevisions.current.get(item.job_id) ?? 0)) return false;
      rowListRevisions.current.set(item.job_id, revision);
      return true;
    });
    for (const item of accepted) {
      const previous = previousById.get(item.job_id);
      if (notifyUpdates && previous && TERMINAL_STATES.has(item.state)
          && (!TERMINAL_STATES.has(previous.state) || item.revision > previous.revision
            || notificationKeysRef.current!.has(notificationKey(item)))) notify(item);
    }
    if (!accepted.length) return;
    const merged = mergeSummaries(itemsRef.current, accepted);
    itemsRef.current = merged;
    setItems(merged);
  }, [notify]);
  const catalog = useResearchTaskCatalog(enabled && catalogVisible, backgroundHead, observeRows);
  const waitingCount = counts.waiting_input + counts.interrupted;
  const waitingPreviewNeeded = items.filter((item) => item.state === "waiting_input" || item.state === "interrupted").length < Math.min(waitingCount, 2);
  useEffect(() => {
    if (!enabled || !quickVisible || !waitingPreviewNeeded) return;
    let active = true;
    // The unfiltered head can be full of queued jobs. Read only the small
    // missing attention preview while the panel is open.
    void listResearchTasks({ limit: 2, states: TASK_FILTER_STATES.attention }).then((result) => {
      if (active) observeRows(result.items, result.revision);
    }).catch(() => { /* Global counts and the full attention view remain usable. */ });
    return () => { active = false; };
  }, [enabled, quickVisible, waitingPreviewNeeded, backgroundHead?.revision, observeRows]);

  const mergeJob = useCallback((job: ResearchJob, notifyTerminal: boolean, updateCounts = true) => {
    const previous = itemsRef.current.find((item) => item.job_id === job.job_id);
    const incoming = summaryFromJob(job, previous);
    if (previous && incoming.revision < previous.revision) return false;
    const terminalIsNew = notifyTerminal && TERMINAL_STATES.has(incoming.state)
      && (!previous || !TERMINAL_STATES.has(previous.state) || incoming.revision > previous.revision);
    const merged = mergeSummaries(itemsRef.current, [incoming]);
    itemsRef.current = merged;
    setItems(merged);
    if (updateCounts) {
      const nextCounts = taskCountsForTransition(countsRef.current, previous?.state, incoming.state);
      countsRef.current = nextCounts;
      setCounts(nextCounts);
    }
    if (terminalIsNew) notify(incoming);
    if (selectedTaskIdRef.current === job.job_id) {
      const current = detailRef.current;
      if (current?.job_id !== job.job_id || (current.revision ?? 0) <= (job.revision ?? 0)) {
        detailRef.current = job;
        setDetail(job);
      }
    }
    return true;
  }, [notify]);

  const applyList = useCallback((result: ResearchTaskList, notifyUpdates: boolean, pageOffset = 0) => {
    if (revisionRef.current !== undefined && result.revision < revisionRef.current) return false;
    observeRows(result.items, result.revision, notifyUpdates);
    revisionRef.current = Math.max(revisionRef.current ?? 0, result.revision);
    setCounts(result.counts ?? emptyCounts());
    countsRef.current = result.counts ?? emptyCounts();
    setQueuePaused(result.queue_paused);
    if (pageOffset === 0) {
      lastHead.current = result;
      setBackgroundHead(result);
    }
    if (pageOffset > 0 || loadedThroughOffsetRef.current === 0) {
      loadedThroughOffsetRef.current = Math.max(loadedThroughOffsetRef.current, pageOffset);
      setHasMore(result.has_more);
      nextOffsetRef.current = result.next_offset;
    }
    return true;
  }, [observeRows]);

  const selectedSummary = items.find((item) => item.job_id === selectedTaskId);
  const selectedState = selectedSummary?.state;
  const settledRevision = selectedState && !POLLED_STATES.has(selectedState) && selectedState !== "queued"
    ? selectedSummary?.revision : 0;
  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    let active = true;
    let timeout = 0;
    let inFlight = false;
    let failures = 0;
    let unchangedPolls = 0;
    let first = true;
    const poll = async () => {
      if (!active || inFlight) return;
      inFlight = true;
      const beforeRevision = revisionRef.current;
      const requestGeneration = listRequestGenerationRef.current;
      try {
        const options: TaskListOptions = {
          limit: PAGE_SIZE,
          offset: 0,
          ...(beforeRevision !== undefined ? { since_revision: beforeRevision } : {}),
        };
        const result = await listResearchTasks(normalizeOptions(options));
        if (!active || requestGeneration !== listRequestGenerationRef.current) return;
        if (beforeRevision !== undefined && result.revision < beforeRevision) return;
        failures = 0;
        setError("");
        const changed = result.revision !== beforeRevision || first;
        unchangedPolls = changed ? 0 : unchangedPolls + 1;
        if (!result.not_modified) {
          const nextIds = new Set(result.items.map((item) => item.job_id));
          const departed = lastHead.current?.items.filter((item) => POLLED_STATES.has(item.state) && !nextIds.has(item.job_id)) ?? [];
          if (applyList(result, initialListReceivedRef.current)) {
            // A just-finished job can fall behind 50 queued jobs. Reconcile
            // the vanished running row once, rather than polling every job.
            for (const item of departed) {
              const id = item.job_id;
              if (detailRequestsRef.current.has(id)) continue;
              detailRequestsRef.current.add(id);
              void getResearchStatus(id).then((job) => {
                if (active && job.job_id === id) mergeJob(job, true, false);
              }).catch(() => undefined).finally(() => detailRequestsRef.current.delete(id));
            }
          }
        }
        if (first) {
          first = false;
          initialListReceivedRef.current = true;
        }
        setLoading(false);
      } catch (reason) {
        if (!active) return;
        failures += 1;
        setLoading(false);
        setError(reason instanceof Error ? reason.message : String(reason ?? "RESEARCH_TASK_LIST_UNAVAILABLE"));
      } finally {
        inFlight = false;
        if (!active) return;
        const delay = failures > 0 ? Math.min(10_000, 1_000 * 2 ** Math.min(failures, 4)) : unchangedPolls > 0 ? 3_000 : 1_000;
        timeout = window.setTimeout(() => void poll(), delay);
      }
    };
    setLoading(itemsRef.current.length === 0 && !initialListReceivedRef.current);
    void poll();
    return () => {
      active = false;
      window.clearTimeout(timeout);
    };
  }, [enabled, refreshVersion, applyList, mergeJob]);

  useEffect(() => {
    if (!enabled || !detailVisible || !selectedTaskId) return;
    const knownDetail = detailRef.current;
    if (selectedState && selectedState !== "queued" && !POLLED_STATES.has(selectedState)
        && knownDetail?.job_id === selectedTaskId && knownDetail.state === selectedState
        && (knownDetail.revision ?? 0) >= (settledRevision ?? 0)) return;
    const generation = selectionGenerationRef.current;
    let active = true;
    let timeout = 0;
    let inFlight = false;
    let consecutiveFailures = 0;
    const poll = async () => {
      if (!active || inFlight || selectedTaskIdRef.current !== selectedTaskId || selectionGenerationRef.current !== generation) return;
      if (detailRequestsRef.current.has(selectedTaskId)) {
        timeout = window.setTimeout(() => void poll(), 1_000);
        return;
      }
      inFlight = true;
      detailRequestsRef.current.add(selectedTaskId);
      try {
        const next = await getResearchStatus(selectedTaskId);
        if (!active || selectedTaskIdRef.current !== selectedTaskId || selectionGenerationRef.current !== generation || next.job_id !== selectedTaskId) return;
        consecutiveFailures = 0;
        setDetailError("");
        const summary = itemsRef.current.find((item) => item.job_id === selectedTaskId);
        const knownRevision = Math.max(summary?.revision ?? 0, detailRef.current?.job_id === selectedTaskId ? detailRef.current.revision ?? 0 : 0);
        const isCurrentRevision = (next.revision ?? 0) >= knownRevision;
        if (isCurrentRevision) mergeJob(next, true);
        const currentState = isCurrentRevision ? next.state : summary?.state;
        if (!isCurrentRevision || (currentState && POLLED_STATES.has(currentState))) timeout = window.setTimeout(() => void poll(), 1_000);
      } catch (reason) {
        if (active && selectedTaskIdRef.current === selectedTaskId && selectionGenerationRef.current === generation) {
          consecutiveFailures += 1;
          setDetailError(reason instanceof Error ? reason.message : String(reason ?? "RESEARCH_TASK_STATUS_UNAVAILABLE"));
          const delay = Math.min(10_000, 1_000 * 2 ** Math.min(consecutiveFailures - 1, 4));
          timeout = window.setTimeout(() => void poll(), delay);
        }
      } finally {
        inFlight = false;
        detailRequestsRef.current.delete(selectedTaskId);
      }
    };
    void poll();
    return () => {
      active = false;
      window.clearTimeout(timeout);
    };
    // `detail` is intentionally read through a ref: detail updates must not create overlapping poll loops.
  }, [enabled, detailVisible, selectedTaskId, selectedState, settledRevision, detailVersion, mergeJob]);

  const acceptJob = useCallback((job: ResearchJob): string => {
    mergeJob(job, true);
    if (selectedTaskIdRef.current === null) setSelectedTaskId(job.job_id);
    setError("");
    setRefreshVersion((value) => value + 1);
    return job.job_id;
  }, [mergeJob, setSelectedTaskId]);

  const refresh = useCallback(() => {
    listRequestGenerationRef.current += 1;
    revisionRef.current = undefined;
    loadedThroughOffsetRef.current = 0;
    nextOffsetRef.current = null;
    setHasMore(false);
    setRefreshVersion((value) => value + 1);
  }, []);

  const loadMore = useCallback(async () => {
    if (!enabled || loadMoreInFlightRef.current || nextOffsetRef.current === null) return;
    loadMoreInFlightRef.current = true;
    const requestGeneration = listRequestGenerationRef.current;
    const offset = nextOffsetRef.current;
    try {
      const result = await listResearchTasks({ limit: PAGE_SIZE, offset });
      if (requestGeneration !== listRequestGenerationRef.current) return;
      if (result.revision < (revisionRef.current ?? 0)) return;
      if (!result.not_modified) applyList(result, false, offset);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason ?? "RESEARCH_TASK_LIST_UNAVAILABLE"));
    } finally {
      loadMoreInFlightRef.current = false;
    }
  }, [applyList, enabled]);

  const refreshDetail = useCallback(() => {
    selectionGenerationRef.current += 1;
    detailRef.current = null;
    setDetail(null);
    setDetailError("");
    setDetailVersion((version) => version + 1);
  }, []);

  const command = useCallback(async <T,>(id: string, action: () => Promise<T>): Promise<T | null> => {
    if (pendingCommands.current.has(id)) return null;
    pendingCommands.current.add(id);
    setCommands((current) => ({ ...current, [id]: { pending: true, error: "" } }));
    let failure = "";
    try {
      return await action();
    } catch (reason) {
      failure = reason instanceof Error ? reason.message : String(reason ?? "RESEARCH_TASK_COMMAND_FAILED");
      if (selectedTaskIdRef.current === id) refreshDetail();
      return null;
    } finally {
      pendingCommands.current.delete(id);
      setCommands((current) => ({ ...current, [id]: { pending: false, error: failure } }));
      refresh();
    }
  }, [refresh, refreshDetail]);

  const taskCommand = (id: string, action: () => Promise<ResearchJob>) => command(id, async () => {
    const next = await action();
    if (next.job_id !== id) throw new Error("RESEARCH_TASK_IDENTITY_MISMATCH");
    mergeJob(next, true);
    return next;
  });
  const cancel = (jobId: string) => taskCommand(jobId, () => cancelResearch(jobId));
  const cancelModel = (jobId: string, requestId: string, attemptId: string) =>
    taskCommand(jobId, () => cancelModelCall(jobId, requestId, attemptId));
  const decideVision = (jobId: string, revision: number, planId: string, approved: boolean) =>
    taskCommand(jobId, () => decideVisionUpload(jobId, approved, revision, planId));
  const resume = (jobId: string, revision: number, model?: ModelReference, useCurrentVisionSettings = false) =>
    taskCommand(jobId, () => resumeResearchTask(jobId, revision, model, useCurrentVisionSettings));
  const approveBudget = (job: ResearchJob, digest: string, gaps: string[], choice?: "keep" | "remove") =>
    taskCommand(job.job_id, async () => {
      const latest = detailRef.current;
      const summary = itemsRef.current.find((item) => item.job_id === job.job_id);
      if (!job.run_id || latest?.job_id !== job.job_id || latest.revision !== job.revision
          || (summary && (summary.state !== job.state || summary.revision > (job.revision ?? 0)))
          || latest.call_plan?.plan_digest !== digest || !["waiting_input", "interrupted"].includes(latest.state)) {
        throw new Error("RESEARCH_TASK_REVISION_CONFLICT");
      }
      if (choice) await extendResearchBudget(job.run_id, digest, gaps, choice);
      else await extendResearchBudget(job.run_id, digest, gaps);
      return resumeResearchTask(job.job_id, job.revision ?? 0);
    });
  const archive = (jobId: string, revision: number) => command(jobId, async () => {
    await archiveResearchTask(jobId, revision);
    const remaining = itemsRef.current.filter((item) => item.job_id !== jobId);
    itemsRef.current = remaining;
    setItems(remaining);
    catalog.remove(jobId);
    if (selectedTaskIdRef.current === jobId) setSelectedTaskId(null);
    return true;
  });
  const continueQueue = () => command("queue", async () => {
    const result = await continueResearchQueue();
    return applyList(result, true);
  });

  const dismissNotification = useCallback((jobId: string) => {
    setNotifications((current) => {
      const target = current.find((item) => item.id === jobId || item.job_id === jobId);
      return target ? current.filter((item) => item.id !== target.id) : current;
    });
    const notification = notifications.find((item) => item.id === jobId || item.job_id === jobId);
    const task = notification ?? itemsRef.current.find((item) => item.job_id === jobId);
    if (task) {
      notificationKeysRef.current!.add(notificationKey(task));
      persistNotificationKeys(notificationKeysRef.current!);
    }
  }, [notifications]);

  return {
    items,
    counts,
    queuePaused,
    loading,
    error,
    hasMore,
    selectedTaskId,
    detail,
    detailError,
    refreshDetail,
    commands,
    catalog,
    selectedSummary,
    notifications,
    dismissNotification,
    selectTask: setSelectedTaskId,
    setSelectedTaskId,
    acceptJob,
    refresh,
    loadMore,
    cancelTask: cancel,
    cancel,
    cancelTaskModel: cancelModel,
    cancelModel,
    decideTaskVision: decideVision,
    decideVisionUpload: decideVision,
    resumeTask: resume,
    resume,
    approveBudget,
    archiveTask: archive,
    archive,
    continueQueue,
  };
}
