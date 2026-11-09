import { ArrowLeft, ArrowUpRight, Clock3, Ellipsis, FileText } from "lucide-react";
import { useId, useState } from "react";
import { openExternalUrl } from "../../backend";
import { FilingPreparationProgress, filingProgressCounts, useResearchElapsed, VisionUploadApproval } from "../../components/States";
import type { Language, ModelCallHistoryPage, ModelReference, ResearchJob } from "../../types";
import { BudgetGapApproval } from "../research/BudgetGapApproval";
import { ModelCallProgress, ResearchDiagnosticsAction } from "../research/ModelCallProgress";
import { formatElapsedTime } from "../research/researchProgress";
import { getResearchTaskCopy } from "./researchTasksCopy";
import { taskCapabilities, taskOperation, taskStage, taskStatus } from "./taskPresentation";

export type TaskModelChoice = {
  configured_model_id: string;
  configuration_version: number;
  connection_id?: string;
  alias: string;
};
export type TaskDetailActions = {
  cancel: (jobId: string) => void;
  resume: (jobId: string, revision: number, model?: ModelReference, useCurrentVisionSettings?: boolean) => void;
  archive: (jobId: string, revision: number) => void;
  openResult: (job: ResearchJob) => void;
  decideVision: (jobId: string, revision: number, planId: string, approved: boolean) => void;
  approveBudget: (job: ResearchJob, digest: string, gaps: string[], choice?: "keep" | "remove") => Promise<void>;
  cancelModel: (jobId: string, requestId: string, attemptId: string) => void;
  configureModels: () => void;
  loadCallHistory: (runId: string, stageId: string, after?: string, limit?: number) => Promise<ModelCallHistoryPage>;
};

const TABS = ["overview", "preparation", "models", "diagnostics"] as const;
type DetailTab = typeof TABS[number];

function modelKey(model: TaskModelChoice) {
  return `${model.configured_model_id}:${model.configuration_version}:${model.connection_id ?? ""}`;
}

function LatestActivity({ job, language }: { job: ResearchJob; language: Language }) {
  const copy = getResearchTaskCopy(language);
  const filings = Object.values(job.filing_states ?? {});
  const lastFilingActivity = filings.flatMap((item) => [item.last_activity_at, item.last_product_at])
    .map((value) => value ? Date.parse(value) : NaN).filter(Number.isFinite).sort((a, b) => b - a)[0];
  const callIdle = (job.model_calls ?? []).filter((call) => call.physical_attempt !== false)
    .map((call) => call.idle_seconds).filter((value): value is number => typeof value === "number" && Number.isFinite(value) && value >= 0);
  const filingIdle = Number.isFinite(lastFilingActivity) ? Math.max(0, (Date.now() - lastFilingActivity) / 1000) : Infinity;
  const idle = Math.min(filingIdle, ...callIdle);
  const ago = language === "en" ? " ago" : language === "zh-Hant" ? " 前" : " 前";
  return <div><dt>{copy.latestActivity}</dt><dd>{Number.isFinite(idle)
    ? <time dateTime={`PT${Math.floor(idle)}S`}>{formatElapsedTime(idle)}{ago}</time> : copy.noActivity}</dd></div>;
}

export function TaskDetail({ job, language, models, actions, pending, refreshing, actionError, onBack }: {
  job: ResearchJob;
  language: Language;
  models: TaskModelChoice[];
  actions: TaskDetailActions;
  pending: boolean;
  refreshing: boolean;
  actionError: string;
  onBack: () => void;
}) {
  const copy = getResearchTaskCopy(language);
  const [tab, setTab] = useState<DetailTab>("overview");
  const [selectedModelKey, setSelectedModelKey] = useState("");
  const tabId = useId();
  const modelId = useId();
  const elapsed = useResearchElapsed(job);
  const caps = taskCapabilities(job, language);
  const selectedModel = models.find((model) => modelKey(model) === selectedModelKey);
  const fileCounts = filingProgressCounts(job);
  const revision = job.revision ?? 0;
  const preview = job.vision_upload_preview;
  const disabled = pending || refreshing;
  const activeCalls = (job.model_calls ?? []).filter((call) =>
    ["queued", "connecting", "connected", "waiting_response", "running", "reasoning", "heartbeat", "content"].includes(call.state));
  const waitingForResponse = job.state === "running" && !job.execution_draining && activeCalls.length > 0
    && activeCalls.every((call) => call.state === "waiting_response" || call.state === "connected");
  const stage = waitingForResponse ? copy.awaitingResponse : taskStage(job, language);
  const resultLabel = caps.remoteUnknown && caps.report ? copy.reportRecovery : caps.report;
  const showResult = Boolean(resultLabel && (caps.ended || caps.remoteUnknown || caps.waiting));
  const resultButton = showResult && <button type="button" className={caps.approval ? "task-button" : "task-button task-button--primary"}
    onClick={() => actions.openResult(job)}><FileText size={15} />{resultLabel}</button>;
  const disclosureButton = caps.disclosure && <button type="button" className="task-button" onClick={async () => {
    try { await openExternalUrl(caps.disclosure!); } catch { /* Optional external navigation must not change the task. */ }
  }}>{copy.openDisclosure}<ArrowUpRight size={15} /></button>;
  const cancel = caps.cancel && <button type="button" className="task-button task-button--danger" disabled={disabled}
    onClick={() => actions.cancel(job.job_id)}>{copy.cancel}</button>;
  const canShowModelCalls = Boolean(job.model_calls?.length || Object.keys(job.agent_states ?? {}).length);
  const nextStep = caps.remoteUnknown || caps.waiting || job.state === "failed" ? caps.reason
    : job.state === "queued" ? copy.queueHint
    : job.state === "cancelling" ? copy.cancellingHint
    : caps.ended ? job.research_complete === true && job.result_access === "saved" ? copy.researchComplete
      : job.result_access === "saved" ? copy.reasons[caps.code] ?? copy.researchIncomplete
      : copy.noSavedResult
    : copy.activeHint;
  const warning = caps.remoteUnknown || job.state === "failed" ? "warning" : caps.waiting ? "attention" : undefined;

  return <>
    <button type="button" className="task-button task-back-to-list" onClick={onBack}><ArrowLeft size={16} />{copy.backToList}</button>
    <header className="task-detail-heading">
      <div>
        <p className="task-overline">{job.company?.ticker || "—"} · {taskOperation(job.operation_type, language)}</p>
        <h2 tabIndex={-1} data-task-detail-focus>{job.company?.name || job.company?.ticker || copy.details}</h2>
      </div>
      <span className="task-state" data-state={job.state} data-partial={job.state === "completed" && job.research_complete !== true || undefined}>
        {taskStatus(job, language)}
      </span>
      {caps.ended && <details className="task-more-actions">
        <summary aria-label={copy.more}><Ellipsis size={20} aria-hidden="true" /></summary>
        <div><button type="button" className="task-button" disabled={disabled} onClick={() => actions.archive(job.job_id, revision)}>{copy.archive}</button></div>
      </details>}
    </header>
    <div className="task-current-activity">
      <strong>{stage}</strong>
      {job.state === "queued" && Number.isInteger(job.ahead_count)
        ? <span>{copy.aheadCount}: {job.ahead_count}</span>
        : <time dateTime={`PT${elapsed}S`}><Clock3 size={15} aria-hidden="true" />{copy.elapsed} {formatElapsedTime(elapsed)}</time>}
    </div>

    <section className="task-next-step" data-tone={warning} aria-label={copy.nextStep}>
      <p role={caps.remoteUnknown ? "alert" : undefined}>{nextStep}</p>
      {caps.ended && job.action_required === true && job.research_complete === true && <p>{copy.actionRequired}</p>}
      {caps.approval === "vision" && (preview?.plan_id && preview.pages.length > 0
        ? <VisionUploadApproval preview={preview} labels={copy.vision} disabled={disabled}
          onDecision={(approved) => actions.decideVision(job.job_id, revision, preview.plan_id!, approved)} />
        : <p role="alert">{copy.visionPlanMissing}</p>)}
      {caps.approval === "model" && <div className="task-model-choice">
        <label htmlFor={modelId}>{copy.chooseModel}</label>
        <select id={modelId} disabled={disabled} value={selectedModelKey} onChange={(event) => setSelectedModelKey(event.target.value)}>
          <option value="">{copy.chooseModel}</option>
          {models.map((model) => <option value={modelKey(model)} key={modelKey(model)}>{model.alias || model.configured_model_id}</option>)}
        </select>
        {!models.length && <button type="button" className="task-button" onClick={actions.configureModels}>{copy.configureModels}</button>}
      </div>}
      {caps.approval === "budget" && job.run_id && <BudgetGapApproval runId={job.run_id} language={language} callPlan={job.call_plan}
        disabled={disabled} onApprove={(digest, gaps, choice) => actions.approveBudget(job, digest, gaps, choice)} />}
      <div className="task-action-row">
        {caps.approval === "continue" && <button type="button" className="task-button task-button--primary" disabled={disabled}
          onClick={() => actions.resume(job.job_id, revision)}>{copy.resume}</button>}
        {caps.approval === "model" && <button type="button" className="task-button task-button--primary" disabled={!selectedModel || disabled}
          onClick={() => selectedModel && actions.resume(job.job_id, revision, {
            configured_model_id: selectedModel.configured_model_id, configuration_version: selectedModel.configuration_version,
            connection_id: selectedModel.connection_id, role: "primary",
          })}>{copy.resumeWithModel}</button>}
        {caps.approval === "vision-settings" && <button type="button" className="task-button task-button--primary" disabled={disabled}
          onClick={() => actions.resume(job.job_id, revision, undefined, true)}>{copy.useVisionSettings}</button>}
        {resultButton}
        {disclosureButton}
        {cancel}
      </div>
      {pending && <small role="status">{copy.actionBusy}</small>}
      {refreshing && !pending && <small role="status">{copy.loading}</small>}
      {actionError && <p role="alert">{/REVISION_CONFLICT|PLAN_CHANGED|GAP_CHANGED/.test(actionError) ? copy.revisionConflict : copy.actionFailed}</p>}
    </section>

    <div className="task-detail-tabs" role="tablist" aria-label={copy.details}>
      {TABS.map((value, index) => <button key={value} id={`${tabId}-${value}`} type="button" role="tab"
        aria-selected={value === tab} aria-controls={`${tabId}-panel`} tabIndex={value === tab ? 0 : -1}
        onClick={() => setTab(value)}
        onKeyDown={(event) => {
          const next = event.key === "ArrowRight" ? (index + 1) % TABS.length
            : event.key === "ArrowLeft" ? (index - 1 + TABS.length) % TABS.length
            : event.key === "Home" ? 0 : event.key === "End" ? TABS.length - 1 : -1;
          if (next < 0) return;
          event.preventDefault();
          setTab(TABS[next]);
          document.getElementById(`${tabId}-${TABS[next]}`)?.focus({ preventScroll: true });
        }}>{copy.tabs[value]}</button>)}
    </div>
    <div id={`${tabId}-panel`} className="task-tab-content" role="tabpanel" aria-labelledby={`${tabId}-${tab}`} tabIndex={0}>
      {tab === "overview" && <dl className="task-overview">
        <div><dt>{copy.model}</dt><dd>{job.model || "—"}</dd></div>
        <LatestActivity job={job} language={language} />
        <div><dt>{copy.materials}</dt><dd>{fileCounts.total ? `${fileCounts.settled} / ${fileCounts.total}` : copy.noProgress}</dd></div>
        <div><dt>{copy.modelStages}</dt><dd>{job.total_agents && job.completed_agents !== undefined ? `${job.completed_agents} / ${job.total_agents}` : copy.noProgress}</dd></div>
      </dl>}
      {tab === "preparation" && <FilingPreparationProgress job={job} language={language} />}
      {tab === "models" && (canShowModelCalls ? <ModelCallProgress calls={job.model_calls ?? []} agentStates={job.agent_states}
        language={language} runId={job.run_id} loadCallHistory={actions.loadCallHistory}
        cancelling={disabled || job.state === "cancelling"}
        onCancelCall={job.state === "running" && !job.execution_draining
          ? (request, attempt) => actions.cancelModel(job.job_id, request, attempt) : undefined} />
        : <p className="task-muted">{copy.noProgress}</p>)}
      {tab === "diagnostics" && <div className="task-diagnostics">
        {job.run_id && <ResearchDiagnosticsAction runId={job.run_id} language={language} />}
        <details>
          <summary>{copy.diagnosticCode}</summary>
          <dl className="task-overview">
            <div><dt>{copy.diagnosticCode}</dt><dd><code>{caps.code || "—"}</code></dd></div>
            <div><dt>{copy.details}</dt><dd><code>{job.job_id}</code></dd></div>
            {job.model_calls?.map((call) => <div key={`${call.request_id}:${call.attempt_id}`}>
              <dt>{call.stage_id}</dt><dd><code>{call.request_id} / {call.attempt_id}</code></dd>
            </div>)}
          </dl>
        </details>
        <dl className="task-overview">
          <LatestActivity job={job} language={language} />
          {([[copy.queuedAt, job.created_at], [copy.startedAt, job.started_at], [copy.finishedAt, job.finished_at]] as const)
            .filter(([, stamp]) => stamp && Number.isFinite(Date.parse(stamp))).map(([label, stamp]) =>
              <div key={label}><dt>{label}</dt><dd><time dateTime={stamp!}>{new Date(stamp!).toLocaleString(language)}</time></dd></div>)}
        </dl>
      </div>}
    </div>
  </>;
}
