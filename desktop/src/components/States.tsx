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

type RemainingRange = { lower: number; upper: number };

function isFilingProgressStalled(job: ResearchJob): boolean {
  if (!["filing-parse", "filing-window"].includes(job.stage ?? "")) return false;
  const stageElapsed = Number(job.stage_elapsed_seconds ?? 0);
  const current = Math.max(0, Number(job.stage_current ?? 0));
  const total = Math.max(0, Number(job.stage_total ?? 0));
  if (!Number.isFinite(stageElapsed) || !Number.isFinite(current) || !Number.isFinite(total)) return false;
  if (total <= 1 || current >= total) return false;
  // A document can legitimately take longer than its neighbours.  Stop
  // extrapolating only after three minutes per reported position; the timer
  // remains visible and the next completed filing restores an evidence-based
  // range automatically.
  return stageElapsed >= Math.max(180, Math.max(1, current) * 180);
}

function estimateRemainingRange(job: ResearchJob, elapsed: number, stalled = false): RemainingRange | null {
  if (job.state !== "running" || job.vision_approval_pending || job.stage === "vision-approval") return null;
  if (stalled) return null;
  const safeElapsed = Math.max(0, Number.isFinite(elapsed) ? elapsed : 0);
  if (safeElapsed <= 0) return null;
  const current = Number(job.stage_current);
  const total = Number(job.stage_total);
  let remaining: number | null = null;
  if (Number.isFinite(current) && Number.isFinite(total) && current > 0 && total > current) {
    const terminalStatuses = new Set(["cache-hit", "validated", "parsed", "blocked", "failed", "cancelled"]);
    const samples = Object.values(job.filing_states ?? {})
      .filter((item) => terminalStatuses.has(item.status))
      .map((item) => Number(item.elapsed_seconds ?? 0))
      .filter((value) => Number.isFinite(value) && value > 0);
    const perItem = samples.length > 0
      ? samples.reduce((sum, value) => sum + value, 0) / samples.length
      : safeElapsed / current;
    remaining = perItem * (total - current);
  } else {
    const percent = Number(job.percent);
    if (Number.isFinite(percent) && percent > 0 && percent < 100) {
      remaining = safeElapsed * (100 - percent) / percent;
    }
  }
  if (remaining === null || !Number.isFinite(remaining) || remaining <= 0) return null;
  const bounded = Math.min(7_200, Math.max(5, remaining));
  return {
    lower: Math.max(5, Math.floor(bounded * 0.6)),
    upper: Math.min(7_200, Math.max(10, Math.ceil(bounded * 1.5))),
  };
}

function formatRemaining(seconds: number, language: string): string {
  if (seconds < 60) return language === "en" ? `${seconds}s` : `${seconds}秒`;
  const minutes = Math.max(1, Math.round(seconds / 60));
  return language === "en" ? `${minutes}m` : `${minutes}${language === "zh-Hant" ? "分鐘" : "分钟"}`;
}

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
  const progressStalled = isFilingProgressStalled(job);
  const remaining = estimateRemainingRange(job, elapsed, progressStalled);
  const remainingLabel = language === "en"
    ? "Estimated remaining"
    : language === "zh-Hant" ? "預計剩餘範圍" : "预计剩余范围";
  const stalledCopy = language === "en"
    ? "Deep parsing is still active. The remaining-time range will return after the next filing completes."
    : language === "zh-Hant"
      ? "仍在深度解析財報。下一份財報完成後，系統會重新估算剩餘時間。"
      : "仍在深度解析财报。下一份财报完成后，系统会重新估算剩余时间。";
  const externalStage = ["filing-download", "vision-approval", "vision-processing", "comparison"].includes(stage ?? "");
  const sinceBackend = Math.max(0, elapsed - backendElapsed);
  const activeElapsed = Math.max(0, Math.floor((job.engine_active_seconds ?? backendElapsed) + (externalStage ? 0 : sinceBackend)));
  const externalElapsed = Math.max(0, Math.floor((job.external_wait_seconds ?? 0) + (externalStage ? sinceBackend : 0)));
  const timingLabels = language === "en"
    ? { total: "Total", active: "Engine", external: "External" }
    : language === "zh-Hant"
      ? { total: "總計", active: "引擎", external: "外部等待" }
      : { total: "总计", active: "引擎", external: "外部等待" };
  const filingStates = Object.values(job.filing_states ?? {});
  const filingStatus = (status: string) => {
    const values: Record<string, readonly [string, string, string]> = {
      queued: ["排队中", "Queued", "排隊中"],
      "cache-check": ["检查缓存", "Checking cache", "檢查快取"],
      "cache-hit": ["缓存命中", "Cache hit", "快取命中"],
      indexing: ["建立页索引", "Indexing", "建立頁面索引"],
      "local-parsing": ["本地解析", "Local parsing", "本地解析"],
      "local-validating": ["本地校验", "Local validating", "本地驗證"],
      "cloud-awaiting-approval": ["等待云端授权", "Awaiting cloud approval", "等待雲端授權"],
      "cloud-processing": ["云端处理中", "Cloud processing", "雲端處理中"],
      "canonical-compiling": ["汇总校验中", "Canonical compiling", "彙總驗證中"],
      validated: ["已验证", "Validated", "已驗證"],
      parsed: ["解析完成", "Parsed", "解析完成"],
      blocked: ["已阻塞", "Blocked", "已阻塞"],
      failed: ["失败", "Failed", "失敗"],
      cancelled: ["已取消", "Cancelled", "已取消"],
    };
    const value = values[status];
    return value ? value[language === "en" ? 1 : language === "zh-Hant" ? 2 : 0] : status;
  };
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
          <time dateTime={`PT${elapsed}S`} aria-label={language === "en" ? "Elapsed research time" : language === "zh-Hant" ? "研究已用時間" : "研究已用时间"}>
            <Clock3 size={14} aria-hidden="true" />{timingLabels.total} {formatElapsedTime(elapsed)}
          </time>
          <time dateTime={`PT${activeElapsed}S`}>{timingLabels.active} {formatElapsedTime(activeElapsed)}</time>
          <time dateTime={`PT${externalElapsed}S`}>{timingLabels.external} {formatElapsedTime(externalElapsed)}</time>
        </div>
      </div>
      <div className="progress-track"><span style={{ transform: `scaleX(${job.percent / 100})` }} /></div>
      <blockquote className="research-waiting-copy" key={waitingMessage}>{waitingMessage}</blockquote>
      {remaining && <div className="research-remaining" aria-label={remainingLabel}>
        <span>{remainingLabel}</span>
        <strong>{formatRemaining(remaining.lower, language)}–{formatRemaining(remaining.upper, language)}</strong>
      </div>}
      {progressStalled && <div className="research-stalled" role="status">{stalledCopy}</div>}
      {filingStates.length > 0 && <div className="filing-progress-list" aria-label={language === "en" ? "Filing progress" : language === "zh-Hant" ? "財報進度" : "财报进度"}>
        {filingStates.map((filing) => <div className="filing-progress-row" key={filing.filing_id} data-status={filing.status}>
          <span>{filing.label}</span>
          <strong>{filingStatus(filing.status)}</strong>
          <time dateTime={`PT${Math.max(0, filing.elapsed_seconds ?? 0)}S`}>{formatElapsedTime(filing.elapsed_seconds ?? 0)}</time>
          {filing.error_code && <code>{filing.error_code}</code>}
        </div>)}
      </div>}
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
