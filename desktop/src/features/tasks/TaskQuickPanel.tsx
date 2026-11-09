import { ArrowRight, ChevronDown, CircleAlert, ListTodo, X } from "lucide-react";
import { useEffect, useId, useLayoutEffect, useRef } from "react";
import type { Language, ResearchTaskCounts, ResearchTaskSummary } from "../../types";
import { getResearchTaskCopy } from "./researchTasksCopy";
import { taskStage, taskStatus, type TaskFilter } from "./taskPresentation";

export function TaskQuickPanel({ language, counts, items, open, onToggle, onOpenTask, onOpenAll }: {
  language: Language;
  counts: ResearchTaskCounts;
  items: ResearchTaskSummary[];
  open: boolean;
  onToggle: (open: boolean) => void;
  onOpenTask: (task: ResearchTaskSummary) => void;
  onOpenAll: (filter?: TaskFilter) => void;
}) {
  const copy = getResearchTaskCopy(language);
  const panelId = useId();
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const toggle = useRef(onToggle);
  toggle.current = onToggle;
  const attention = counts.waiting_input + counts.interrupted;
  const running = counts.running + counts.cancelling;
  const label = attention > 0 ? `${copy.attention} ${attention}`
    : `${copy.states.running} ${running} · ${copy.states.queued} ${counts.queued}`;
  const close = () => {
    onToggle(false);
    trigger.current?.focus({ preventScroll: true });
  };
  useLayoutEffect(() => {
    if (open) closeButton.current?.focus({ preventScroll: true });
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      toggle.current(false);
      trigger.current?.focus({ preventScroll: true });
    };
    const handleOutside = (event: PointerEvent) => {
      if (event.target instanceof Node && !root.current?.contains(event.target)) toggle.current(false);
    };
    window.addEventListener("keydown", handleEscape, true);
    window.addEventListener("pointerdown", handleOutside);
    return () => {
      window.removeEventListener("keydown", handleEscape, true);
      window.removeEventListener("pointerdown", handleOutside);
    };
  }, [open]);
  const groups = [
    { id: "attention", filter: "attention", count: attention, label: copy.attention,
      tasks: items.filter((task) => task.state === "waiting_input" || task.state === "interrupted") },
    { id: "running", filter: "active", count: running, label: copy.states.running,
      tasks: items.filter((task) => task.state === "running" || task.state === "cancelling") },
    { id: "queued", filter: "active", count: counts.queued, label: copy.states.queued,
      tasks: items.filter((task) => task.state === "queued").sort((a, b) => (a.queue_position ?? Infinity) - (b.queue_position ?? Infinity)) },
  ] as const;
  return <div className="task-quick-anchor" ref={root}>
    <button type="button" ref={trigger} className="task-quick-trigger" aria-haspopup="dialog"
      aria-expanded={open} aria-controls={open ? panelId : undefined} aria-label={`${copy.quick} · ${label}`}
      data-attention={attention > 0 || undefined} onClick={() => onToggle(!open)}>
      {attention > 0 ? <CircleAlert size={16} /> : <ListTodo size={16} />}
      <span>{label}</span><ChevronDown size={14} />
    </button>
    {open && <section id={panelId} className="task-quick-panel" role="dialog" aria-modal="false" aria-label={copy.quick}>
      <header><h2>{copy.quick}</h2><button ref={closeButton} type="button" className="task-icon-action" aria-label={copy.close} onClick={close}><X size={18} /></button></header>
      <div className="task-quick-body">
        {groups.filter((group) => group.count > 0 || group.tasks.length > 0).map((group) => <section key={group.id} className="task-quick-group">
          <h3>{group.label} <span>{group.count}</span></h3>
          <ul>{group.tasks.slice(0, 2).map((task) => <li key={task.job_id}>
            <button type="button" onClick={() => onOpenTask(task)}>
              <span><strong>{task.company.name || task.company.ticker}</strong><small>{task.state === "queued" && Number.isInteger(task.queue_position)
                ? `${copy.queuePosition} ${task.queue_position} · ${copy.aheadCount} ${task.ahead_count ?? "—"}`
                : taskStage(task, language)}</small></span>
              <span className="task-state" data-state={task.state}>{taskStatus(task, language)}</span>
              <ArrowRight size={15} />
            </button>
          </li>)}</ul>
          {group.count > Math.min(2, group.tasks.length) && <button type="button" className="task-quick-more" onClick={() => onOpenAll(group.filter)}>
            {copy.filters[group.filter]} <ArrowRight size={14} />
          </button>}
        </section>)}
        {groups.every((group) => group.count === 0 && group.tasks.length === 0) && <p className="task-muted">{copy.empty}</p>}
      </div>
      <footer><button type="button" className="task-button task-button--primary" onClick={() => onOpenAll()}>{copy.allTasks}<ArrowRight size={15} /></button></footer>
    </section>}
  </div>;
}
