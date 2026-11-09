import { useLayoutEffect, useRef, useState } from "react";
import { ListTodo, RotateCcw } from "lucide-react";
import type { Language, ResearchJob, ResearchTaskCounts, ResearchTaskSummary } from "../../types";
import { formatElapsedTime } from "../research/researchProgress";
import { TaskDetail, type TaskDetailActions, type TaskModelChoice } from "./TaskDetail";
import { getResearchTaskCopy } from "./researchTasksCopy";
import { matchesTaskFilter, TASK_FILTERS, taskOperation, taskStage, taskStatus, type TaskFilter } from "./taskPresentation";
import "./researchTasks.css";

export type ResearchTaskCenterProps = {
  language: Language;
  catalog: {
    filter: TaskFilter;
    selectFilter: (filter: TaskFilter) => void;
    items: ResearchTaskSummary[];
    loading: boolean;
    error: string;
    hasMore: boolean;
    refresh: () => void;
    pageNumber: number;
    hasPrevious: boolean;
    previousPage: () => void;
    nextPage: () => void;
  };
  counts: ResearchTaskCounts;
  queuePaused: boolean;
  queuePending: boolean;
  queueError: string;
  onContinueQueue: () => void;
  selectedTaskId: string | null;
  selectedSummary?: ResearchTaskSummary;
  detail: ResearchJob | null;
  detailError: string;
  actionPending: boolean;
  actionError: string;
  onSelect: (id: string) => void;
  onReloadDetail: () => void;
  openDetailToken: number;
  actions: TaskDetailActions;
  models: TaskModelChoice[];
};

export function ResearchTaskCenter({
  language, catalog, counts, queuePaused, queuePending, queueError, onContinueQueue,
  selectedTaskId, selectedSummary, detail, detailError, actionPending, actionError,
  onSelect, onReloadDetail, openDetailToken, actions, models,
}: ResearchTaskCenterProps) {
  const copy = getResearchTaskCopy(language);
  const [showDetail, setShowDetail] = useState(false);
  const list = useRef<HTMLElement>(null);
  const detailPane = useRef<HTMLElement>(null);
  const listScroll = useRef(0);
  const lastOpenToken = useRef(0);
  const focusIntent = useRef<"list" | "detail" | null>(null);
  const selected = detail?.job_id === selectedTaskId ? detail : null;
  const selectedState = selectedSummary?.state ?? selected?.state;
  const outsideFilter = selectedState !== undefined && !matchesTaskFilter(selectedState, catalog.filter);
  const stale = Boolean(selected && selectedSummary && (
    selectedSummary.state !== selected.state
    || (selected.state !== "running" && (selected.revision ?? 0) < selectedSummary.revision)
  ));
  const displayed = selected?.state === "queued" && selectedSummary?.state === "queued"
    ? { ...selected, ahead_count: selectedSummary.ahead_count, queue_position: selectedSummary.queue_position } : selected;
  const filterCounts = { active: counts.running + counts.queued + counts.cancelling, attention: counts.waiting_input + counts.interrupted };
  const items = catalog.items.filter((item) => matchesTaskFilter(item.state, catalog.filter));

  useLayoutEffect(() => {
    if (lastOpenToken.current === openDetailToken) return;
    lastOpenToken.current = openDetailToken;
    focusIntent.current = "detail";
    setShowDetail(true);
  }, [openDetailToken]);

  useLayoutEffect(() => {
    if (list.current && list.current.clientHeight) list.current.scrollTop = listScroll.current;
    if (!showDetail && focusIntent.current === "list") {
      list.current?.querySelector<HTMLButtonElement>('[aria-pressed="true"]')?.focus({ preventScroll: true });
      focusIntent.current = null;
    }
    if (showDetail && focusIntent.current === "detail" && detailPane.current?.clientHeight) {
      // Only move focus for an explicit move into the single-pane detail.
      const back = detailPane.current.querySelector<HTMLButtonElement>(".task-back-to-list");
      if (back?.getClientRects().length) (detailPane.current.querySelector<HTMLElement>("[data-task-detail-focus]") ?? back).focus({ preventScroll: true });
      focusIntent.current = null;
    }
  }, [showDetail, selected?.job_id, openDetailToken]);

  const open = (id: string) => {
    if (list.current?.clientHeight) listScroll.current = list.current.scrollTop;
    if (detailPane.current && selectedTaskId !== id) detailPane.current.scrollTop = 0;
    focusIntent.current = "detail";
    setShowDetail(true);
    onSelect(id);
  };
  const scrollListToStart = () => {
    listScroll.current = 0;
    if (list.current) list.current.scrollTop = 0;
  };

  return <section className="research-task-center" aria-label={copy.title} data-show-detail={showDetail && Boolean(selectedTaskId) || undefined}>
    <div className="task-filters" role="group" aria-label={copy.filtersLabel}>
      {TASK_FILTERS.map((filter) => <button type="button" key={filter} aria-pressed={filter === catalog.filter}
        onClick={() => {
          if (filter === catalog.filter) return;
          scrollListToStart();
          setShowDetail(false);
          catalog.selectFilter(filter);
        }}>{copy.filters[filter]}
        {(filter === "active" || filter === "attention") && <span className="task-filter-count">{filterCounts[filter]}</span>}
      </button>)}
    </div>
    {queuePaused && <div className="task-queue-notice" role="status">
      <span>{copy.queuePaused}</span>
      <button type="button" className="task-button" disabled={queuePending} onClick={onContinueQueue}>{copy.continueQueue}</button>
    </div>}
    {queueError && <p className="task-list-error" role="alert">{copy.actionFailed}</p>}
    <div className="task-center-grid">
      <section ref={list} className="task-list-pane" aria-label={copy.allTasks}
        onScroll={(event) => { if (event.currentTarget.clientHeight) listScroll.current = event.currentTarget.scrollTop; }}>
        {catalog.error && <div className="task-list-error" role="alert">
          <span>{copy.error}</span>
          <button type="button" className="task-button" onClick={catalog.refresh}><RotateCcw size={14} />{copy.retryList}</button>
        </div>}
        {catalog.loading && <p className="task-muted" role="status">{copy.loading}</p>}
        {!catalog.loading && !catalog.error && items.length === 0 && <p className="task-muted">{catalog.hasPrevious
          ? copy.emptyPage : catalog.filter === "all" ? copy.empty : copy.noMatching}</p>}
        {items.length > 0 && <ul className="task-list">
          {items.map((item) => <li key={item.job_id}>
            <button type="button" className="task-list-row" aria-pressed={selectedTaskId === item.job_id} onClick={() => open(item.job_id)}>
              <span className="task-row-title"><strong>{item.company.name || item.company.ticker}</strong>
                <span className="task-state" data-state={item.state} data-partial={item.state === "completed" && item.research_complete !== true || undefined}>
                  {taskStatus(item, language)}</span></span>
              <span className="task-row-meta">{[item.company.ticker, taskOperation(item.operation_type, language)].filter(Boolean).join(" · ")}</span>
              <span className="task-row-stage">{item.state === "queued" && Number.isInteger(item.queue_position)
                ? `${copy.queuePosition} ${item.queue_position} · ${copy.aheadCount} ${item.ahead_count ?? "—"}`
                : taskStage(item, language)}</span>
              <span className="task-row-meta">
                <span title={item.model}>{item.model || "—"}</span>
                {item.state !== "queued" && <time dateTime={`PT${Math.max(0, item.elapsed_seconds)}S`}>{formatElapsedTime(item.elapsed_seconds)}</time>}
              </span>
            </button>
          </li>)}
        </ul>}
        {(catalog.hasPrevious || catalog.hasMore) && <nav className="task-pagination" aria-label={copy.pagination}>
          <button type="button" className="task-button" disabled={!catalog.hasPrevious}
            onClick={() => { scrollListToStart(); catalog.previousPage(); }}>{copy.previousPage}</button>
          <span role="status">{copy.pageNumber.replace("{n}", String(catalog.pageNumber))}</span>
          <button type="button" className="task-button" disabled={!catalog.hasMore || catalog.loading}
            onClick={() => { scrollListToStart(); catalog.nextPage(); }}>{copy.nextPage}</button>
        </nav>}
      </section>
      <section className="task-detail-pane" ref={detailPane} aria-label={copy.details}>
        {outsideFilter && <p className="task-outside-filter" role="status">{copy.outsideFilter}</p>}
        {!selectedTaskId && <div className="task-detail-empty"><ListTodo size={24} /><p>{copy.selectHint}</p></div>}
        {selectedTaskId && !selected && <div className="task-detail-loading">
          <button type="button" className="task-button task-back-to-list" onClick={() => { focusIntent.current = "list"; setShowDetail(false); }}>{copy.backToList}</button>
          {actionError && <p role="alert">{/REVISION_CONFLICT|PLAN_CHANGED|GAP_CHANGED/.test(actionError) ? copy.revisionConflict : copy.actionFailed}</p>}
          {detailError ? <div role="alert"><p>{copy.detailsUnavailable}</p>
            <button type="button" className="task-button" onClick={onReloadDetail}>{copy.reloadDetail}</button></div>
            : <p className="task-muted" role="status">{copy.loading}</p>}
        </div>}
        {displayed && <TaskDetail key={displayed.job_id} job={displayed} language={language} models={models}
          actions={actions} pending={actionPending} refreshing={stale} actionError={actionError}
          onBack={() => { focusIntent.current = "list"; setShowDetail(false); }} />}
      </section>
    </div>
  </section>;
}
