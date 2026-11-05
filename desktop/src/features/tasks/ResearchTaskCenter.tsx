import { useEffect, useState, type ReactNode } from "react";
import { ResearchProgress } from "../../components/States";
import { openExternalUrl } from "../../backend";
import { formatElapsedTime, progressStageCopy } from "../research/researchProgress";
import type { Language, ModelReference, ResearchJob, ResearchTaskCounts, ResearchTaskSummary } from "../../types";
import { getResearchTaskCopy } from "./researchTasksCopy";
import "./researchTasks.css";

export type TaskModelChoice = {
  configured_model_id: string;
  configuration_version: number;
  connection_id?: string;
  alias: string;
};

export type ResearchTaskCenterProps = {
  variant: "drawer" | "page";
  language: Language;
  items: ResearchTaskSummary[];
  counts: ResearchTaskCounts;
  queuePaused: boolean;
  loading: boolean;
  error: string;
  hasMore: boolean;
  selectedTaskId: string | null;
  detail: ResearchJob | null;
  onSelect: (id: string) => void;
  onCancel: (id: string) => void;
  onResume: (id: string, revision: number, model?: ModelReference) => void;
  onArchive: (id: string, revision: number) => void;
  onViewReport: (runId: string) => void;
  onClose: () => void;
  onFullPage: () => void;
  onContinueQueue: () => void;
  onLoadMore: () => void;
  onCancelModel: (jobId: string, requestId: string, attemptId: string) => void;
  onVisionDecision: (jobId: string, revision: number, planId: string, approved: boolean) => void;
  models?: TaskModelChoice[];
  loadCallHistory?: Parameters<typeof ResearchProgress>[0]["loadCallHistory"];
  children?: ReactNode;
};

const TERMINAL_STATES = new Set(["completed", "failed", "cancelled"]);
const CONFIGURATION_FAILURE = /MODEL_(?:CONFIGURATION_CHANGED|CONFIGURATION_INVALID|CONNECTION_AUTH_PAUSED|CREDENTIAL_(?:MISSING|INVALID)|UNAUTHORIZED|AUTHENTICATION_ERROR|GATEWAY_CONFIGURATION_ERROR|DISABLED)|NEEDS_CONFIGURATION/i;
const REMOTE_UNKNOWN = /MODEL_REMOTE_RESULT_UNKNOWN|REMOTE_UNKNOWN|REMOTE_STATUS_UNCONFIRMED|OUTCOME_UNKNOWN/i;
const CODE_REASON = /^[A-Z][A-Z0-9_.-]{2,}$/;
const OFFICIAL_DISCLOSURE_DOMAINS = ["sec.gov", "cninfo.com.cn", "hkexnews.hk"] as const;

function isRemoteUnknown(job: ResearchJob, reason: string): boolean {
  return [reason, job.blocked_reason, job.error_code, job.recovery_outcome?.diagnostic_code]
    .some((candidate) => REMOTE_UNKNOWN.test(candidate ?? "")) || Boolean(job.model_calls?.some(
    (call) => call.state === "remote_unknown" || call.remote_status_unconfirmed,
  ));
}

function stateLabel(state: string, labels: Record<string, string>, fallback: string): string {
  return labels[state] ?? fallback;
}

function readableReason(reason: string, fallback: string, labels: Record<string, string>): string {
  const code = reason.trim().toUpperCase();
  if (!code) return fallback;
  if (labels[code]) return labels[code];
  return CODE_REASON.test(code) ? fallback : reason;
}

function readableOutcome(job: ResearchJob, fallback: string, labels: Record<string, string>): string {
  const code = (job.recovery_outcome?.diagnostic_code || job.error_code || job.blocked_reason || "").trim().toUpperCase();
  if (labels[code]) return labels[code];
  const message = job.message.trim();
  if (message && !CODE_REASON.test(message.toUpperCase())) return message;
  return fallback;
}

function officialDisclosureUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    const hostname = url.hostname.toLowerCase();
    const isOfficialHost = OFFICIAL_DISCLOSURE_DOMAINS.some((domain) => hostname === domain || hostname.endsWith(`.${domain}`));
    return url.protocol === "https:" && !url.port && !url.username && !url.password && isOfficialHost ? url.href : null;
  } catch {
    return null;
  }
}

function operationLabel(operation: string, labels: Record<string, string>): string {
  const normalized = operation.trim().toLowerCase();
  return labels[normalized] ?? (operation.replace(/[-_]+/g, " ") || labels.research || operation);
}

function modelKey(model: TaskModelChoice): string {
  return `${model.configured_model_id}:${model.configuration_version}:${model.connection_id ?? ""}`;
}

function modelReference(choice: TaskModelChoice): ModelReference {
  return {
    configured_model_id: choice.configured_model_id,
    configuration_version: choice.configuration_version,
    connection_id: choice.connection_id,
    role: "primary",
  };
}

function safeSeconds(seconds: number): number {
  return Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
}

export function ResearchTaskCenter({
  variant, language, items, counts, queuePaused, loading, error, hasMore,
  selectedTaskId, detail, onSelect, onCancel, onResume, onArchive, onViewReport,
  onClose, onFullPage, onContinueQueue, onLoadMore, onCancelModel, onVisionDecision,
  models = [], loadCallHistory, children,
}: ResearchTaskCenterProps) {
  const copy = getResearchTaskCopy(language);
  const selectedDetail = selectedTaskId && detail?.job_id === selectedTaskId ? detail : null;
  const selectedSummary = items.find((item) => item.job_id === selectedTaskId) ?? null;
  const revision = selectedDetail?.revision ?? selectedSummary?.revision ?? 0;
  const reason = selectedDetail?.blocked_reason || selectedSummary?.blocked_reason || selectedDetail?.error_code || "";
  const remoteUnknown = selectedDetail ? isRemoteUnknown(selectedDetail, reason) : false;
  const needsModelSelection = CONFIGURATION_FAILURE.test(reason);
  const recoveryCode = selectedDetail?.recovery_outcome?.diagnostic_code?.trim() ?? "";
  const diagnosticCode = recoveryCode || selectedDetail?.error_code || (CODE_REASON.test(reason.trim().toUpperCase()) ? reason.trim() : "");
  const displayedReason = readableReason(reason || selectedDetail?.message || "", copy.genericReason, copy.reasons);
  const planId = selectedDetail?.vision_upload_preview?.plan_id ?? "";
  const [selectedModelKey, setSelectedModelKey] = useState("");
  const selectedModel = models.find((model) => modelKey(model) === selectedModelKey) ?? null;

  useEffect(() => setSelectedModelKey(""), [selectedTaskId]);
  useEffect(() => {
    if (variant !== "drawer") return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [variant, onClose]);

  const canContinue = Boolean(selectedDetail
    && ["waiting_input", "interrupted"].includes(selectedDetail.state)
    && !remoteUnknown
    && !selectedDetail.vision_approval_pending
    && (!needsModelSelection || selectedModel));
  const canCancel = Boolean(selectedDetail && ["queued", "running", "waiting_input", "interrupted"].includes(selectedDetail.state));
  const terminal = Boolean(selectedDetail && TERMINAL_STATES.has(selectedDetail.state));
  const showOutcome = Boolean(selectedDetail && (terminal || selectedDetail.error_code || selectedDetail.recovery_outcome
    || selectedDetail.action_required || (["waiting_input", "interrupted"].includes(selectedDetail.state)
      && selectedDetail.research_complete === false)));
  const attemptComplete = selectedDetail?.attempt_complete === true;
  const researchIncomplete = selectedDetail?.research_complete === false;
  const researchComplete = selectedDetail?.research_complete === true;
  const actionRequired = selectedDetail?.action_required === true;
  const outcomeMessage = selectedDetail ? readableOutcome(selectedDetail, copy.genericReason, copy.reasons) : "";
  const hasRecoveryActions = Boolean(selectedDetail?.recovery_outcome?.next_actions?.length
    || selectedDetail?.research_complete === false || selectedDetail?.action_required);
  const disclosureUrl = officialDisclosureUrl(selectedDetail?.disclosure_url);
  const cancelling = selectedDetail?.state === "cancelling";
  const showDetailActions = canContinue || canCancel || cancelling || terminal;
  const reportRunId = selectedDetail?.run_id ?? selectedSummary?.run_id ?? null;

  const submitResume = () => {
    if (!selectedDetail || !canContinue) return;
    onResume(selectedDetail.job_id, revision, needsModelSelection && selectedModel
      ? modelReference(selectedModel)
      : undefined);
  };

  const cancelSelected = () => {
    if (!selectedDetail || !canCancel) return;
    onCancel(selectedDetail.job_id);
  };

  const submitVisionDecision = (approved: boolean) => {
    if (!selectedDetail || !planId || !selectedDetail.vision_approval_pending) return;
    onVisionDecision(selectedDetail.job_id, revision, planId, approved);
  };

  return (
    <aside
      className={`research-task-center research-task-center--${variant}`}
      data-variant={variant}
      aria-label={copy.title}
    >
      <header className="research-task-center__header">
        <div className="research-task-center__title">
          <h2>{copy.title}</h2>
          <span className="research-task-center__count" aria-label={copy.status}>
            {counts.running + counts.queued + counts.waiting_input + counts.interrupted + counts.cancelling}
          </span>
        </div>
        <div className="research-task-center__header-actions">
          {variant === "drawer" && <button type="button" className="research-task-center__secondary" onClick={() => onFullPage()}>{copy.openPage}</button>}
          <button type="button" className="research-task-center__close" aria-label={copy.close} onClick={() => onClose()}>×</button>
        </div>
      </header>

      {queuePaused && <div className="research-task-center__queue-paused" role="status">
        <span>{copy.queuePaused}</span>
        <button type="button" onClick={() => onContinueQueue()}>{copy.continueQueue}</button>
      </div>}
      {error && <p className="research-task-center__error" role="alert">{copy.error}: {error}</p>}

      <div className="research-task-center__main">
        <section className="research-task-center__list" aria-label={copy.title}>
          {loading && <p className="research-task-center__status" role="status">{copy.loading}</p>}
          {!loading && items.length === 0 && <p className="research-task-center__status">{copy.empty}</p>}
          {items.length > 0 && <ul>
            {items.map((item) => (
              <li key={item.job_id}>
                <button
                  type="button"
                  className="research-task-center__task"
                  aria-pressed={selectedTaskId === item.job_id}
                  aria-current={selectedTaskId === item.job_id ? "true" : undefined}
                  onClick={() => onSelect(item.job_id)}
                >
                  <span className="research-task-center__task-heading">
                    <strong>{item.company.name || item.company.ticker || item.job_id}</strong>
                    {item.company.ticker && <span>{item.company.ticker}</span>}
                  </span>
                  <span className="research-task-center__task-meta">
                    <span>{operationLabel(item.operation_type, copy.operations)}</span>
                    <span className={`research-task-center__state research-task-center__state--${item.state}`}>
                      {stateLabel(item.state, copy.states, item.state)}
                    </span>
                  </span>
                  <span className="research-task-center__task-meta">
                    <span>{copy.model}: {item.model || "—"}</span>
                    {item.state === "queued" && item.queue_position !== undefined
                      ? <span>{copy.queuePosition} {item.queue_position}</span>
                      : <time dateTime={`PT${safeSeconds(item.elapsed_seconds)}S`}>{copy.elapsed} {formatElapsedTime(safeSeconds(item.elapsed_seconds))}</time>}
                  </span>
                  <span className="research-task-center__task-stage">{copy.stage}: {progressStageCopy(language, item.stage).title}</span>
                </button>
              </li>
            ))}
          </ul>}
          {hasMore && <button type="button" className="research-task-center__load-more" disabled={loading} onClick={() => onLoadMore()}>{copy.loadMore}</button>}
          {children}
        </section>

        <section
          className="research-task-center__detail"
          aria-label={copy.details}
          data-terminal={terminal ? "true" : "false"}
          data-cancelable={canCancel ? "true" : "false"}
        >
          {!selectedTaskId && <p className="research-task-center__status">{copy.selectHint}</p>}
          {selectedTaskId && !selectedDetail && <p className="research-task-center__status" role="status">{loading ? copy.loading : copy.selectHint}</p>}
          {selectedDetail && <>
            <div className="research-task-center__detail-heading">
              <div>
                <h3>{selectedDetail.company?.name || selectedSummary?.company.name || selectedDetail.job_id}</h3>
                <p>{selectedDetail.company?.ticker || selectedSummary?.company.ticker || ""}
                  {selectedDetail.operation_type && <> · {operationLabel(selectedDetail.operation_type, copy.operations)}</>}
                </p>
              </div>
              <span className={`research-task-center__state research-task-center__state--${selectedDetail.state}`}>
                {stateLabel(selectedDetail.state, copy.states, selectedDetail.state)}
              </span>
            </div>

            {(selectedDetail.state === "waiting_input" || selectedDetail.state === "interrupted") && <div className="research-task-center__reason" role="status">
              <strong>{selectedDetail.state === "interrupted" ? copy.interruptedReason : copy.waitingReason}</strong>
              <span>{displayedReason}</span>
              {diagnosticCode && <small>{copy.diagnosticCode}: {diagnosticCode}</small>}
            </div>}

            {showOutcome && <div className="research-task-center__reason" role="status">
              <strong>{copy.outcomeTitle}</strong>
              <span>{outcomeMessage}</span>
              {attemptComplete && <span>{copy.attemptComplete}</span>}
              {researchIncomplete && <span>{copy.researchIncomplete}</span>}
              {researchComplete && <span>{copy.researchComplete}</span>}
              {actionRequired && <span>{copy.actionRequired}</span>}
              {hasRecoveryActions && <span>{copy.recoveryNextStep}</span>}
              {diagnosticCode && <small>{copy.diagnosticCode}: {diagnosticCode}</small>}
              {hasRecoveryActions && reportRunId && !remoteUnknown && <button type="button" onClick={() => onViewReport(reportRunId)}>{copy.reportRecovery}</button>}
              {disclosureUrl && <button type="button" onClick={() => {
                try {
                  void openExternalUrl(disclosureUrl).catch(() => undefined);
                } catch {
                  // Keep an external-link adapter failure local to this optional action.
                }
              }}>{copy.openDisclosure}</button>}
            </div>}

            {remoteUnknown && <div className="research-task-center__warning" role="alert">
              <p>{copy.remoteUnknown}</p>
              {reportRunId && <button type="button" onClick={() => onViewReport(reportRunId)}>{copy.reportRecovery}</button>}
            </div>}

            {needsModelSelection && !remoteUnknown && ["waiting_input", "interrupted"].includes(selectedDetail.state) && <div className="research-task-center__model-recovery">
              <label htmlFor="research-task-model">{copy.chooseModel}</label>
              <p>{copy.chooseModelHint}</p>
              <select id="research-task-model" value={selectedModelKey} onChange={(event) => setSelectedModelKey(event.target.value)}>
                <option value="">{copy.chooseModel}</option>
                {models.map((model) => <option key={modelKey(model)} value={modelKey(model)}>{model.alias || model.configured_model_id}</option>)}
              </select>
              {!selectedModel && <small>{models.length ? copy.modelRequired : copy.chooseModelHint}</small>}
              <button type="button" disabled={!canContinue} onClick={submitResume}>{copy.resumeWithModel}</button>
            </div>}

            {selectedDetail.vision_approval_pending && !planId && <p className="research-task-center__warning" role="alert">{copy.visionPlanMissing}</p>}

            {(!selectedDetail.vision_approval_pending || planId) && <div className="research-task-center__progress">
              <ResearchProgress
                language={language}
                job={selectedDetail}
                showCancel={false}
                loadCallHistory={loadCallHistory}
                onCancel={cancelSelected}
                onVisionDecision={planId ? submitVisionDecision : undefined}
                onCancelCall={(requestId, attemptId) => onCancelModel(selectedDetail.job_id, requestId, attemptId)}
                labels={{
                  cancel: copy.cancel, cancelling: copy.cancelling, agents: language === "en" ? "Agents" : "研究代理",
                  running: copy.states.running, retrying: language === "en" ? "Retrying separately" : "单独重试中",
                  queued: copy.states.queued, completed: copy.states.completed, cancelled: copy.states.cancelled,
                  failed: copy.states.failed, unknown: copy.states.waiting_input,
                  visionApprovalTitle: language === "en" ? "Vision upload approval" : language === "zh-Hant" ? "視覺上傳授權" : "视觉上传授权",
                  visionApprovalProvider: language === "en" ? "Provider" : language === "zh-Hant" ? "供應商" : "供应商",
                  visionApprovalDocument: language === "en" ? "Document" : language === "zh-Hant" ? "文件" : "文件",
                  visionApprovalPages: language === "en" ? "Pages" : language === "zh-Hant" ? "頁面" : "页面",
                  visionApprovalSize: language === "en" ? "Size" : language === "zh-Hant" ? "大小" : "大小",
                  visionApprovalFingerprint: language === "en" ? "Fingerprint" : language === "zh-Hant" ? "指紋" : "指纹",
                  visionApprovalApprove: language === "en" ? "Approve upload" : language === "zh-Hant" ? "同意上傳" : "同意上传",
                  visionApprovalDecline: language === "en" ? "Do not upload" : language === "zh-Hant" ? "拒絕上傳" : "拒绝上传",
                }}
              />
            </div>}

            {showDetailActions && <div className="research-task-center__detail-actions">
              {(canCancel || cancelling) && <button type="button" className="research-task-center__danger" disabled={cancelling} onClick={cancelSelected}>{cancelling ? copy.cancelling : copy.cancel}</button>}
              {canContinue && !needsModelSelection && <button type="button" className="research-task-center__primary" onClick={submitResume}>{copy.resume}</button>}
              {terminal && reportRunId && <button type="button" className="research-task-center__secondary" onClick={() => onViewReport(reportRunId)}>{copy.viewReport}</button>}
              {terminal && <button type="button" className="research-task-center__secondary" onClick={() => onArchive(selectedDetail.job_id, revision)}>{copy.archive}</button>}
            </div>}
          </>}
        </section>
      </div>
    </aside>
  );
}
