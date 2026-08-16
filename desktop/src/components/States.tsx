import { useEffect, useRef, useState } from "react";
import { Clock3, FileText } from "lucide-react";

import type { ResearchJob } from "../types";
import {
  agentDisplayName,
  formatElapsedTime,
  progressStageCopy,
  progressStageDetail,
  waitingMessageAt,
} from "../features/research/researchProgress";

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

export function ResearchProgress({ job, cancelLabel, labels, language = "zh-CN", onCancel, onVisionDecision }: {
  job: ResearchJob;
  cancelLabel?: string;
  language?: string;
  labels?: {
    cancel: string;
    cancelling: string;
    agents: string;
    running: string;
    retrying: string;
    queued: string;
    completed: string;
    cancelled: string;
    failed: string;
    unknown: string;
    visionApprovalTitle?: string;
    visionApprovalProvider?: string;
    visionApprovalDocument?: string;
    visionApprovalPages?: string;
    visionApprovalSize?: string;
    visionApprovalFingerprint?: string;
    visionApprovalApprove?: string;
    visionApprovalDecline?: string;
  };
  onCancel: () => void;
  onVisionDecision?: (approved: boolean) => void;
}) {
  const copy = labels ?? {
    cancel: cancelLabel ?? "Cancel research",
    cancelling: "Stopping…",
    agents: "Agents",
    running: "Running",
    retrying: "Retrying separately",
    queued: "Queued",
    completed: "Completed",
    cancelled: "Cancelled",
    failed: "Failed",
    unknown: "Waiting",
  };
  const statusLabel = (state: string) => {
    if (state === "running") return copy.running;
    if (state === "retrying") return copy.retrying;
    if (state === "queued") return copy.queued;
    if (state === "completed") return copy.completed;
    if (state === "cancelled") return copy.cancelled;
    if (state === "failed") return copy.failed;
    return copy.unknown;
  };
  const agentStates = job.agent_states ?? {};
  const totalAgents = job.total_agents ?? Object.keys(agentStates).length;
  const completedAgents = job.completed_agents ?? Object.values(agentStates).filter((state) => state === "completed").length;
  const backendElapsed = Math.max(0, job.elapsed_seconds ?? 0);
  const [elapsed, setElapsed] = useState(backendElapsed);
  const elapsedAnchor = useRef({
    jobId: job.job_id,
    seconds: backendElapsed,
    observedAt: Date.now(),
  });
  useEffect(() => {
    const now = Date.now();
    const anchor = elapsedAnchor.current;
    const estimated = anchor.seconds + Math.max(0, Math.floor((now - anchor.observedAt) / 1_000));
    if (anchor.jobId !== job.job_id || backendElapsed > estimated) {
      elapsedAnchor.current = { jobId: job.job_id, seconds: backendElapsed, observedAt: now };
    }
    setElapsed((current) => Math.max(current, backendElapsed));
  }, [backendElapsed, job.job_id]);
  useEffect(() => {
    const now = Date.now();
    elapsedAnchor.current = { jobId: job.job_id, seconds: backendElapsed, observedAt: now };
    setElapsed(backendElapsed);
    const update = () => {
      const anchor = elapsedAnchor.current;
      const actual = anchor.seconds + Math.max(0, Math.floor((Date.now() - anchor.observedAt) / 1_000));
      setElapsed((current) => Math.max(current, actual));
    };
    const timer = window.setInterval(update, 1_000);
    document.addEventListener("visibilitychange", update);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", update);
    };
  }, [job.job_id]);
  const stage = job.state === "cancelling" ? "cancelling" : job.stage;
  const stageCopy = progressStageCopy(language, stage);
  const stageDetail = progressStageDetail(
    language,
    stage,
    job.stage_current ?? undefined,
    job.stage_total ?? undefined,
  );
  const waitingMessage = waitingMessageAt(language, elapsed, job.job_id);
  return (
    <section className="research-progress" data-state={job.state}>
      <div className="research-progress-header">
        <div className="research-stage-copy" aria-live="polite">
          <strong>{stageCopy.title}</strong>
          <span>“{stageCopy.note}”</span>
          {stageDetail && <small>{stageDetail}</small>}
        </div>
        <div className="research-progress-metrics">
          <span>{job.percent}%</span>
          <time dateTime={`PT${elapsed}S`} aria-label={language === "en" ? "Elapsed research time" : "研究已用时间"}>
            <Clock3 size={14} aria-hidden="true" />{formatElapsedTime(elapsed)}
          </time>
        </div>
      </div>
      <div className="progress-track"><span style={{ transform: `scaleX(${job.percent / 100})` }} /></div>
      <blockquote className="research-waiting-copy" key={waitingMessage}>{waitingMessage}</blockquote>
      {totalAgents > 0 && <div className="agent-progress" aria-label={copy.agents}>
        <div className="agent-progress-heading"><span>{copy.agents}</span><span>{completedAgents}/{totalAgents}</span></div>
        <div className="agent-progress-list">
          {Object.entries(agentStates).map(([agentId, state]) => (
            <div className="agent-progress-row" key={agentId}>
              <span className={`agent-state-dot agent-state-${state}`} aria-hidden="true" />
              <span>{agentDisplayName(language, agentId)}</span><span>{statusLabel(state)}</span>
            </div>
          ))}
        </div>
      </div>}
      {job.vision_approval_pending && job.vision_upload_preview && <div className="vision-approval-card" role="dialog" aria-label={copy.visionApprovalTitle ?? "Vision upload approval"}>
        <strong>{copy.visionApprovalTitle ?? "Vision upload approval"}</strong>
        <span>{copy.visionApprovalProvider ?? "Provider"}: {job.vision_upload_preview.provider}</span><span>{copy.visionApprovalDocument ?? "Document"}: {job.vision_upload_preview.source_document || "—"}</span>
        <span>{copy.visionApprovalPages ?? "Pages"}: {job.vision_upload_preview.pages.join(", ")} · {copy.visionApprovalSize ?? "Size"}: {Math.round(job.vision_upload_preview.total_bytes / 1024)} KB</span>
        {job.vision_upload_preview.filing_hash && <code aria-label={copy.visionApprovalFingerprint ?? "Fingerprint"}>{copy.visionApprovalFingerprint ?? "Fingerprint"}: {job.vision_upload_preview.filing_hash.slice(0, 12)}</code>}
        <div className="vision-approval-actions">
          <button type="button" onClick={() => onVisionDecision?.(true)}>{copy.visionApprovalApprove ?? "Approve upload"}</button>
          <button type="button" onClick={() => onVisionDecision?.(false)}>{copy.visionApprovalDecline ?? "Do not upload"}</button>
          <button type="button" onClick={onCancel}>{copy.cancel}</button>
        </div>
      </div>}
      <button type="button" onClick={onCancel} disabled={job.state === "cancelling" || Boolean(job.vision_approval_pending)} hidden={Boolean(job.vision_approval_pending)}>
        {job.state === "cancelling" ? copy.cancelling : copy.cancel ?? cancelLabel}
      </button>
    </section>
  );
}
