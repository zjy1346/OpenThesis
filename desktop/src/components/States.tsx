import { FileText } from "lucide-react";

import type { ResearchJob } from "../types";

export function LoadingState({ label }: { label: string }) {
  return <div className="center-state" aria-live="polite"><span className="loading-ring" /><p>{label}</p></div>;
}

export function EmptyState({ title, body, demoAction, realAction, hint, onDemo, onReal }: {
  title: string;
  body: string;
  demoAction: string;
  realAction: string;
  hint: string;
  onDemo: () => void;
  onReal: () => void;
}) {
  return (
    <div className="center-state empty-state">
      <span className="empty-icon"><FileText size={28} strokeWidth={1.5} /></span>
      <h2>{title}</h2><p>{body}</p>
      <div className="empty-actions">
        <button className="primary-button" type="button" onClick={onDemo}>{demoAction}</button>
        <button className="secondary-button" type="button" onClick={onReal}>{realAction}</button>
      </div>
      <small>{hint}</small>
    </div>
  );
}

export function ResearchProgress({ job, cancelLabel, labels, onCancel }: {
  job: ResearchJob;
  cancelLabel?: string;
  labels?: {
    cancel: string;
    cancelling: string;
    agents: string;
    running: string;
    queued: string;
    completed: string;
    cancelled: string;
    failed: string;
    unknown: string;
  };
  onCancel: () => void;
}) {
  const copy = labels ?? {
    cancel: cancelLabel ?? "Cancel research",
    cancelling: "Stopping…",
    agents: "Agents",
    running: "Running",
    queued: "Queued",
    completed: "Completed",
    cancelled: "Cancelled",
    failed: "Failed",
    unknown: "Waiting",
  };
  const statusLabel = (state: string) => {
    if (state === "running") return copy.running;
    if (state === "queued") return copy.queued;
    if (state === "completed") return copy.completed;
    if (state === "cancelled") return copy.cancelled;
    if (state === "failed") return copy.failed;
    return copy.unknown;
  };
  const agentStates = job.agent_states ?? {};
  const totalAgents = job.total_agents ?? Object.keys(agentStates).length;
  const completedAgents = job.completed_agents ?? Object.values(agentStates).filter((state) => state === "completed").length;
  return (
    <section className="research-progress" aria-live="polite" data-state={job.state}>
      <div><strong>{job.message}</strong><span>{job.percent}%</span></div>
      <div className="progress-track"><span style={{ transform: `scaleX(${job.percent / 100})` }} /></div>
      {totalAgents > 0 && <div className="agent-progress" aria-label={copy.agents}>
        <div className="agent-progress-heading"><span>{copy.agents}</span><span>{completedAgents}/{totalAgents}</span></div>
        <div className="agent-progress-list">
          {Object.entries(agentStates).map(([agentId, state]) => (
            <div className="agent-progress-row" key={agentId}>
              <span className={`agent-state-dot agent-state-${state}`} aria-hidden="true" />
              <span>{agentId}</span><span>{statusLabel(state)}</span>
            </div>
          ))}
        </div>
      </div>}
      <button type="button" onClick={onCancel} disabled={job.state === "cancelling"}>
        {job.state === "cancelling" ? copy.cancelling : copy.cancel ?? cancelLabel}
      </button>
    </section>
  );
}
