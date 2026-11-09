import { useCallback, useEffect, useRef, useState } from "react";
import { listResearchTasks } from "../backend";
import { TASK_FILTER_STATES, type TaskFilter } from "../features/tasks/taskPresentation";
import type { ResearchTaskList, ResearchTaskSummary } from "../types";

const PAGE_SIZE = 50;
type TaskQuery = { filter: TaskFilter; offset: number };

function listOptions(filter: TaskFilter, offset: number, since?: number) {
  const states = TASK_FILTER_STATES[filter];
  return { limit: PAGE_SIZE, offset, ...(states ? { states } : {}),
    ...(since !== undefined ? { since_revision: since } : {}) };
}

/** A single server page, refreshed by the existing background revision.
 * A stable first-page order is not evidence that a later page is unchanged.
 * Read only the currently visible page, never reload a prefix of task history.
 */
export function useResearchTaskCatalog(
  visible: boolean,
  backgroundHead: ResearchTaskList | null,
  observe: (rows: ResearchTaskSummary[], revision: number) => void,
) {
  const [query, setQuery] = useState<TaskQuery>({ filter: "active", offset: 0 });
  const [page, setPage] = useState<ResearchTaskList | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [refreshVersion, setRefreshVersion] = useState(0);
  const latestQuery = useRef(query);
  const current = useRef<ResearchTaskList | null>(null);
  const inFlight = useRef<{ query: TaskQuery } | null>(null);
  const latestBackground = useRef(backgroundHead);
  latestBackground.current = backgroundHead;
  const forceRefresh = useRef(false);
  const update = useCallback((next: ResearchTaskList | null) => {
    current.current = next;
    setPage(next);
  }, []);

  const move = useCallback((next: TaskQuery) => {
    if (latestQuery.current.filter === next.filter && latestQuery.current.offset === next.offset) return;
    latestQuery.current = next;
    forceRefresh.current = false;
    update(null);
    setError("");
    setQuery(next);
  }, [update]);

  const selectFilter = useCallback((filter: TaskFilter) => {
    if (filter !== latestQuery.current.filter) move({ filter, offset: 0 });
  }, [move]);
  const nextPage = useCallback(() => {
    const offset = current.current?.next_offset;
    if (offset != null) move({ filter: latestQuery.current.filter, offset });
  }, [move]);
  const previousPage = useCallback(() => {
    const { filter, offset } = latestQuery.current;
    if (offset > 0) move({ filter, offset: Math.max(0, offset - PAGE_SIZE) });
  }, [move]);

  const refresh = useCallback(() => {
    forceRefresh.current = true;
    setRefreshVersion((value) => value + 1);
  }, []);

  useEffect(() => () => {
    if (inFlight.current?.query === query) inFlight.current = null;
  }, [visible, query]);

  useEffect(() => {
    if (!visible) return;
    if (query !== latestQuery.current || inFlight.current?.query === query) return;
    const previous = current.current;
    const forced = forceRefresh.current;
    if (!forced && previous && backgroundHead && previous.revision >= backgroundHead.revision) return;
    forceRefresh.current = false;
    const request = { query };
    inFlight.current = request;
    const isCurrent = () => inFlight.current === request && query === latestQuery.current;
    const observedRevision = backgroundHead?.revision ?? -1;
    let readRevision = previous?.revision ?? -1;
    setLoading(!previous);
    const read = async () => {
      try {
        const result = query.filter === "all" && query.offset === 0 && backgroundHead && !forced
          ? backgroundHead
          : await listResearchTasks(listOptions(query.filter, query.offset, forced ? undefined : previous?.revision));
        if (!isCurrent() || forceRefresh.current || result.revision < (current.current?.revision ?? 0)) return;
        readRevision = result.revision;
        if (!result.not_modified) {
          observe(result.items, result.revision);
          update(result);
        }
        setError("");
      } catch (reason) {
        if (isCurrent()) setError(reason instanceof Error ? reason.message : "RESEARCH_TASK_LIST_UNAVAILABLE");
      } finally {
        if (isCurrent()) {
          inFlight.current = null;
          setLoading(false);
          // Keep one page read in flight. Coalesce newer revisions observed
          // while it was pending into one follow-up, if the reply is older.
          if (forceRefresh.current || (latestBackground.current?.revision ?? -1) > Math.max(observedRevision, readRevision)) {
            setRefreshVersion((value) => value + 1);
          }
        }
      }
    };
    void read();
  }, [visible, query, backgroundHead, observe, refreshVersion, update]);

  const remove = useCallback((id: string) => {
    const before = current.current;
    if (!before) return;
    update({ ...before, items: before.items.filter((item) => item.job_id !== id) });
    refresh();
  }, [refresh, update]);

  return { filter: query.filter, selectFilter, items: page?.items ?? [],
    pageNumber: Math.floor(query.offset / PAGE_SIZE) + 1, hasPrevious: query.offset > 0, previousPage,
    loading: loading || (visible && !page && !error), error,
    hasMore: page?.next_offset != null, nextPage, refresh, remove };
}
