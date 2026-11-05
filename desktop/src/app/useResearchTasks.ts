import { useCallback, useEffect, useRef, useState } from "react";

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
import type {
  ModelReference,
  ResearchJob,
  ResearchTaskCounts,
  ResearchTaskList,
  ResearchTaskState,
  ResearchTaskSummary,
} from "../types";

const PAGE_SIZE = 50;
const NOTIFICATION_STORAGE_KEY = "openthesis.research-task-notifications.v1";
const TERMINAL_STATES = new Set<ResearchTaskState>(["completed", "failed", "cancelled"]);
const POLLED_STATES = new Set<ResearchTaskState>(["running", "cancelling"]);

export type ResearchTaskNotification = Pick<
  ResearchTaskSummary,
  "job_id" | "run_id" | "state" | "revision" | "company" | "operation_type" | "message"
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

export function useResearchTasks(enabled: boolean) {
  const [items, setItems] = useState<ResearchTaskSummary[]>([]);
  const [counts, setCounts] = useState<ResearchTaskCounts>(emptyCounts);
  const [queuePaused, setQueuePaused] = useState(false);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState("");
  const [hasMore, setHasMore] = useState(false);
  const [selectedTaskId, setSelectedTaskIdState] = useState<string | null>(null);
  const [detail, setDetail] = useState<ResearchJob | null>(null);
  const [notifications, setNotifications] = useState<ResearchTaskNotification[]>([]);
  const [refreshVersion, setRefreshVersion] = useState(0);
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

  itemsRef.current = items;
  countsRef.current = counts;
  detailRef.current = detail;
  if (notificationKeysRef.current === null) notificationKeysRef.current = readNotificationKeys();

  const setSelectedTaskId = useCallback((id: string | null) => {
    if (selectedTaskIdRef.current === id) return;
    selectionGenerationRef.current += 1;
    selectedTaskIdRef.current = id;
    setSelectedTaskIdState(id);
    setDetail(null);
  }, []);

  const mergeJob = useCallback((job: ResearchJob, notifyTerminal: boolean) => {
    const previous = itemsRef.current.find((item) => item.job_id === job.job_id);
    const incoming = summaryFromJob(job, previous);
    if (previous && incoming.revision < previous.revision) return false;
    const terminalIsNew = notifyTerminal && TERMINAL_STATES.has(incoming.state)
      && (!previous || !TERMINAL_STATES.has(previous.state) || incoming.revision > previous.revision);
    const merged = mergeSummaries(itemsRef.current, [incoming]);
    itemsRef.current = merged;
    setItems(merged);
    const nextCounts = taskCountsForTransition(countsRef.current, previous?.state, incoming.state);
    countsRef.current = nextCounts;
    setCounts(nextCounts);
    if (terminalIsNew) {
      const key = notificationKey(incoming);
      const seen = notificationKeysRef.current!;
      if (!seen.has(key)) {
        seen.add(key);
        persistNotificationKeys(seen);
        setNotifications((current) => current.some((item) => notificationKey(item) === key)
          ? current
          : [...current, {
            id: key,
            job_id: incoming.job_id,
            run_id: incoming.run_id,
            state: incoming.state,
            revision: incoming.revision,
            company: incoming.company,
            operation_type: incoming.operation_type,
            message: incoming.message,
          }]);
      }
    }
    if (selectedTaskIdRef.current === job.job_id) {
      setDetail((current) => current?.job_id === job.job_id && (current.revision ?? 0) > (job.revision ?? 0) ? current : job);
    }
    return true;
  }, []);

  const applyList = useCallback((result: ResearchTaskList, notifyUpdates: boolean, pageOffset = 0) => {
    if (revisionRef.current !== undefined && result.revision < revisionRef.current) return false;
    const previousItems = itemsRef.current;
    const nextItems = mergeSummaries(previousItems, result.items);
    if (notifyUpdates) {
      const previousById = new Map(previousItems.map((item) => [item.job_id, item]));
      for (const item of result.items) {
        const previous = previousById.get(item.job_id);
        if (!previous || item.revision < previous.revision || !TERMINAL_STATES.has(item.state)) continue;
        if (TERMINAL_STATES.has(previous.state) && item.revision <= previous.revision) continue;
        const key = notificationKey(item);
        const seen = notificationKeysRef.current!;
        if (seen.has(key)) continue;
        seen.add(key);
        persistNotificationKeys(seen);
        setNotifications((current) => current.some((notification) => notificationKey(notification) === key)
          ? current
          : [...current, {
            id: key,
            job_id: item.job_id,
            run_id: item.run_id,
            state: item.state,
            revision: item.revision,
            company: item.company,
            operation_type: item.operation_type,
            message: item.message,
          }]);
      }
    }
    itemsRef.current = nextItems;
    setItems(nextItems);
    revisionRef.current = Math.max(revisionRef.current ?? 0, result.revision);
    setCounts(result.counts ?? emptyCounts());
    countsRef.current = result.counts ?? emptyCounts();
    setQueuePaused(result.queue_paused);
    if (pageOffset > 0 || loadedThroughOffsetRef.current === 0) {
      loadedThroughOffsetRef.current = Math.max(loadedThroughOffsetRef.current, pageOffset);
      setHasMore(result.has_more);
      nextOffsetRef.current = result.next_offset;
    }
    return true;
  }, []);

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
        if (!result.not_modified) applyList(result, initialListReceivedRef.current);
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
  }, [enabled, refreshVersion, applyList]);

  useEffect(() => {
    if (!enabled || !selectedTaskId) return;
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
        setError("");
        const summary = itemsRef.current.find((item) => item.job_id === selectedTaskId);
        const knownRevision = Math.max(summary?.revision ?? 0, detailRef.current?.job_id === selectedTaskId ? detailRef.current.revision ?? 0 : 0);
        const isCurrentRevision = (next.revision ?? 0) >= knownRevision;
        if (isCurrentRevision) mergeJob(next, true);
        const currentState = isCurrentRevision ? next.state : summary?.state;
        if (currentState && POLLED_STATES.has(currentState)) timeout = window.setTimeout(() => void poll(), 1_000);
      } catch (reason) {
        if (active && selectedTaskIdRef.current === selectedTaskId && selectionGenerationRef.current === generation) {
          consecutiveFailures += 1;
          setError(reason instanceof Error ? reason.message : String(reason ?? "RESEARCH_TASK_STATUS_UNAVAILABLE"));
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
  }, [enabled, selectedTaskId, selectedState, settledRevision, mergeJob]);

  const acceptJob = useCallback((job: ResearchJob): string => {
    mergeJob(job, true);
    setSelectedTaskId(job.job_id);
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

  const cancel = useCallback(async (jobId: string) => {
    try {
      const next = await cancelResearch(jobId);
      mergeJob(next, true);
      refresh();
      setError("");
      return next;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason ?? "RESEARCH_TASK_CANCEL_FAILED"));
      return null;
    }
  }, [mergeJob, refresh]);

  const cancelModel = useCallback(async (jobId: string, requestId: string, attemptId: string) => {
    try {
      const next = await cancelModelCall(jobId, requestId, attemptId);
      mergeJob(next, true);
      refresh();
      setError("");
      return next;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason ?? "RESEARCH_MODEL_CALL_CANCEL_FAILED"));
      return null;
    }
  }, [mergeJob, refresh]);

  const decideVision = useCallback(async (jobId: string, revision: number, planId: string, approved: boolean) => {
    try {
      const next = await decideVisionUpload(jobId, approved, revision, planId);
      mergeJob(next, true);
      refresh();
      setError("");
      return next;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason ?? "RESEARCH_VISION_DECISION_FAILED"));
      return null;
    }
  }, [mergeJob, refresh]);

  const resume = useCallback(async (jobId: string, revision: number, model?: ModelReference, useCurrentVisionSettings = false) => {
    try {
      const next = await resumeResearchTask(jobId, revision, model, useCurrentVisionSettings);
      mergeJob(next, true);
      refresh();
      setError("");
      return next;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason ?? "RESEARCH_TASK_RESUME_FAILED"));
      return null;
    }
  }, [mergeJob, refresh]);

  const archive = useCallback(async (jobId: string, revision: number) => {
    try {
      await archiveResearchTask(jobId, revision);
      setItems((current) => {
        const next = current.filter((item) => item.job_id !== jobId);
        itemsRef.current = next;
        return next;
      });
      if (selectedTaskIdRef.current === jobId) setSelectedTaskId(null);
      refresh();
      setError("");
      return true;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason ?? "RESEARCH_TASK_ARCHIVE_FAILED"));
      return false;
    }
  }, [refresh, setSelectedTaskId]);

  const continueQueue = useCallback(async () => {
    const requestGeneration = listRequestGenerationRef.current;
    try {
      const result = await continueResearchQueue();
      if (requestGeneration !== listRequestGenerationRef.current || !applyList(result, true)) return false;
      setError("");
      return true;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason ?? "RESEARCH_QUEUE_CONTINUE_FAILED"));
      return false;
    }
  }, [applyList]);

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
    archiveTask: archive,
    archive,
    continueQueue,
  };
}
