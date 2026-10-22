import { useEffect, useRef, useState } from "react";
import { Clock3, FileText } from "lucide-react";

import type { ModelCallHistoryPage, ResearchJob } from "../types";
import { ModelCallProgress } from "../features/research/ModelCallProgress";
import {
  formatElapsedTime,
  progressStageCopy,
  progressStageDetail,
  waitingMessageAt,
} from "../features/research/researchProgress";

type RemainingRange = { lower: number; upper: number };
type ProgressTab = "preparation" | "research";

const RESEARCH_STAGES = new Set([
  "parallel-agents", "model-stages", "financial-analysis", "base-analysis", "business-analysis",
  "source-industry-analysis", "source-risk-analysis", "risk-analysis", "growth-analysis",
  "counter-analysis", "scenario-analysis", "synthesis", "final-synthesis", "comparison",
]);
const RESEARCH_AGENT_IDS = new Set([
  "financial-analyst", "business-analyst", "accounting-risk-analyst", "source-industry-analysis",
  "source-risk-analysis", "growth-opportunity-analyst", "skeptical-analyst", "forecast-analyst",
  "research-synthesizer", "growth-opportunities", "counter-analysis", "forecast-scenarios", "final-synthesis",
]);
const FINAL_FILING_STATUSES = new Set(["usable", "complete", "none", "partial", "historical", "not-applicable", "needs-action", "needs_action"]);

function hasModelResearchActivity(job: ResearchJob): boolean {
  if (RESEARCH_STAGES.has(job.stage ?? "") || Object.keys(job.agent_states ?? {}).length > 0) return true;
  return (job.model_calls ?? []).some((call) => RESEARCH_AGENT_IDS.has(call.stage_id)
    || call.stage_id.startsWith("research-"));
}

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

export function ResearchProgress({ job, cancelLabel, labels, language = "zh-CN", onCancel, onVisionDecision, onCancelCall, loadCallHistory }: {
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
  onCancelCall?: (requestId: string, attemptId: string) => void;
  loadCallHistory?: (runId: string, stageId: string, after?: string, limit?: number) => Promise<ModelCallHistoryPage>;
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
  const modelResearchActivity = hasModelResearchActivity(job);
  const [activeTab, setActiveTab] = useState<ProgressTab>("preparation");
  const tabJobId = useRef(job.job_id);
  const autoSwitchedJobId = useRef<string | null>(null);
  useEffect(() => {
    if (tabJobId.current !== job.job_id) {
      tabJobId.current = job.job_id;
      autoSwitchedJobId.current = null;
      setActiveTab("preparation");
    }
    if (modelResearchActivity && autoSwitchedJobId.current !== job.job_id) {
      autoSwitchedJobId.current = job.job_id;
      setActiveTab("research");
    }
  }, [job.job_id, modelResearchActivity]);
  const tabLabels = language === "en"
    ? { preparation: "Preparation", research: "Model research" }
    : language === "zh-Hant"
      ? { preparation: "資料準備", research: "模型研究" }
      : { preparation: "资料准备", research: "模型研究" };
  const agentStates = job.agent_states ?? {};
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
      "window-checkpointed": ["窗口已保存", "Window saved", "視窗已保存"],
      "window-cache-hit": ["窗口已恢复", "Window restored", "視窗已恢復"],
      "window-failed": ["窗口失败", "Window failed", "視窗失敗"],
      "cloud-awaiting-approval": ["等待云端授权", "Awaiting cloud approval", "等待雲端授權"],
      "cloud-processing": ["云端处理中", "Cloud processing", "雲端處理中"],
      "canonical-compiling": ["汇总校验中", "Canonical compiling", "彙總驗證中"],
      validated: ["处理完成", "Processing complete", "處理完成"],
      parsed: ["解析完成", "Parsed", "解析完成"],
      blocked: ["需要处理", "Needs attention", "需要處理"],
      failed: ["失败", "Failed", "失敗"],
      cancelled: ["已取消", "Cancelled", "已取消"],
      processing: ["处理中", "Processing", "處理中"],
      completed: ["处理完成", "Processing complete", "處理完成"],
      complete: ["处理完成", "Processing complete", "處理完成"],
      validating: ["整理与核验中", "Processing and checking", "整理與核驗中"],
    };
    const value = values[status];
    return value ? value[language === "en" ? 1 : language === "zh-Hant" ? 2 : 0] : status;
  };
  const availabilityLabel = (availability: string) => {
    const values: Record<string, readonly [string, string, string]> = {
      usable: ["有可用内容", "Usable content available", "有可用內容"],
      partial: ["部分可用", "Partially usable", "部分可用"],
      complete: ["有可用内容", "Usable content available", "有可用內容"],
      none: ["无可用内容", "No usable content", "無可用內容"],
      historical: ["历史/比较资料已处理", "Historical/comparative material processed", "歷史／比較資料已處理"],
      "not-applicable": ["不适用", "Not applicable", "不適用"],
      "needs-action": ["需要处理", "Needs action", "需要處理"],
      needs_action: ["需要处理", "Needs action", "需要處理"],
    };
    const value = values[availability];
    return value ? value[language === "en" ? 1 : language === "zh-Hant" ? 2 : 0] : availability;
  };
  const fileMetadataLabels = language === "en"
    ? { roles: "Roles", candidates: "Candidates", admitted: "admitted", hash: "Source hash", parseDetails: "Parsing details",
      pages: "Total pages", nativePages: "Native-text pages", indexedPages: "Indexed pages", windowsComplete: "Windows complete",
      parseElapsed: "Parse time", attempts: "Attempts", cacheReused: "Cached result reused", yes: "Yes", no: "No",
      recognizedStatements: "Recognized statements", extractedPages: "Pages with extracted facts", failureStage: "Failure stage", rootError: "Root error code" }
    : language === "zh-Hant"
      ? { roles: "角色", candidates: "候選", admitted: "已納入", hash: "來源雜湊", parseDetails: "解析詳情",
        pages: "總頁數", nativePages: "含原生文字頁數", indexedPages: "已建立索引頁數", windowsComplete: "已完成視窗",
        parseElapsed: "解析耗時", attempts: "嘗試次數", cacheReused: "重用快取結果", yes: "是", no: "否",
        recognizedStatements: "已識別報表", extractedPages: "含擷取事實的頁面", failureStage: "失敗階段", rootError: "根錯誤代碼" }
      : { roles: "角色", candidates: "候选", admitted: "已纳入", hash: "来源哈希", parseDetails: "解析详情",
        pages: "总页数", nativePages: "含原生文本页数", indexedPages: "已建立索引页数", windowsComplete: "已完成窗口",
        parseElapsed: "解析耗时", attempts: "尝试次数", cacheReused: "复用缓存结果", yes: "是", no: "否",
        recognizedStatements: "已识别报表", extractedPages: "含提取事实的页面", failureStage: "失败阶段", rootError: "根错误代码" };
  const roleNames: Record<string, readonly [string, string, string]> = {
    target: ["目标公司", "Target", "目標公司"],
    comparator: ["对照公司", "Comparator", "比較公司"],
    historical: ["历史资料", "Historical", "歷史資料"],
  };
  const roleLabel = (role: string) => roleNames[role]?.[language === "en" ? 1 : language === "zh-Hant" ? 2 : 0] ?? role;
  return (
    <section className="research-progress" data-state={job.state}>
      <div className="research-progress-summary">
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
      </div>
      <button className="research-progress-cancel" type="button" onClick={onCancel} disabled={job.state === "cancelling"}>
        {job.state === "cancelling" ? copy.cancelling : copy.cancel ?? cancelLabel}
      </button>
      {job.vision_approval_pending && job.vision_upload_preview && <div className="vision-approval-card" role="dialog" aria-label={copy.visionApprovalTitle ?? "Vision upload approval"}>
        <strong>{copy.visionApprovalTitle ?? "Vision upload approval"}</strong>
        <span>{copy.visionApprovalProvider ?? "Provider"}: {job.vision_upload_preview.provider}</span><span>{copy.visionApprovalDocument ?? "Document"}: {job.vision_upload_preview.source_document || "—"}</span>
        <span>{copy.visionApprovalPages ?? "Pages"}: {job.vision_upload_preview.pages.join(", ")} · {copy.visionApprovalSize ?? "Size"}: {Math.round(job.vision_upload_preview.total_bytes / 1024)} KB</span>
        {job.vision_upload_preview.filing_hash && <code aria-label={copy.visionApprovalFingerprint ?? "Fingerprint"}>{copy.visionApprovalFingerprint ?? "Fingerprint"}: {job.vision_upload_preview.filing_hash.slice(0, 12)}</code>}
        <div className="vision-approval-actions">
          <button type="button" onClick={() => onVisionDecision?.(true)}>{copy.visionApprovalApprove ?? "Approve upload"}</button>
          <button type="button" onClick={() => onVisionDecision?.(false)}>{copy.visionApprovalDecline ?? "Do not upload"}</button>
        </div>
      </div>}
      <div className="research-progress-tabs" role="tablist" aria-label={language === "en" ? "Research progress views" : language === "zh-Hant" ? "研究進度檢視" : "研究进度视图"}>
        {(["preparation", "research"] as const).map((tab) => <button
          id={`research-progress-tab-${tab}`} key={tab} type="button" role="tab"
          aria-selected={activeTab === tab} aria-controls="research-progress-panel"
          className={activeTab === tab ? "active" : ""} onClick={() => setActiveTab(tab)}
        >{tabLabels[tab]}</button>)}
      </div>
      <div id="research-progress-panel" className="research-progress-viewport" role="tabpanel"
        aria-labelledby={`research-progress-tab-${activeTab}`} tabIndex={0}>
        {activeTab === "preparation" ? <div className="filing-progress-list" aria-label={language === "en" ? "Filing progress" : language === "zh-Hant" ? "財報進度" : "财报进度"}>
          {filingStates.length > 0 ? filingStates.map((filing) => {
            const inferredAvailability = FINAL_FILING_STATUSES.has(filing.status) ? filing.status : undefined;
            const availability = filing.availability ?? inferredAvailability;
            const processStatus = filing.processing_state ?? (availability ? "" : filing.status);
            const issueCodes = [...new Set([...(filing.issue_codes ?? []), ...(filing.error_code ? [filing.error_code] : [])])];
            const hasCounts = filing.candidate_count !== undefined || filing.admitted_count !== undefined;
            const detailRows: [string, string][] = [];
            const numberText = (value: number) => value.toLocaleString(language === "en" ? "en-US" : "zh-CN");
            if (filing.page_count !== undefined) detailRows.push([fileMetadataLabels.pages, numberText(filing.page_count)]);
            if (filing.native_text_page_count !== undefined) detailRows.push([fileMetadataLabels.nativePages, numberText(filing.native_text_page_count)]);
            if (filing.indexed_page_count !== undefined) detailRows.push([fileMetadataLabels.indexedPages, numberText(filing.indexed_page_count)]);
            if (filing.completed_window_count !== undefined || filing.window_total !== undefined) {
              detailRows.push([fileMetadataLabels.windowsComplete, `${numberText(filing.completed_window_count ?? 0)} / ${numberText(filing.window_total ?? 0)}`]);
            }
            if (filing.parse_elapsed_seconds !== undefined) detailRows.push([fileMetadataLabels.parseElapsed, formatElapsedTime(filing.parse_elapsed_seconds)]);
            if (filing.attempt_count !== undefined) detailRows.push([fileMetadataLabels.attempts, numberText(filing.attempt_count)]);
            if (filing.cache_reused !== undefined) detailRows.push([fileMetadataLabels.cacheReused, filing.cache_reused ? fileMetadataLabels.yes : fileMetadataLabels.no]);
            if (filing.recognized_statements?.length) detailRows.push([fileMetadataLabels.recognizedStatements, filing.recognized_statements.join(" · ")]);
            if (filing.extracted_pages?.length) detailRows.push([fileMetadataLabels.extractedPages, filing.extracted_pages.join(", ")]);
            if (filing.failure_stage) detailRows.push([fileMetadataLabels.failureStage, filing.failure_stage]);
            if (filing.root_error_code) detailRows.push([fileMetadataLabels.rootError, filing.root_error_code]);
            return <article className="filing-progress-row" key={filing.filing_id} data-status={filing.status} data-availability={availability}>
              <div className="filing-progress-main">
                <strong className="filing-progress-name" title={filing.label}>{filing.label}</strong>
                <div className="filing-progress-badges">
                  {processStatus && <span className="filing-progress-process">{filingStatus(processStatus)}</span>}
                  {availability && <span className="filing-progress-availability" data-availability={availability}>{availabilityLabel(availability)}</span>}
                </div>
              </div>
              {filing.roles && filing.roles.length > 0 && <small>{fileMetadataLabels.roles}: {filing.roles.map(roleLabel).join(" · ")}</small>}
              {hasCounts && <small>{fileMetadataLabels.candidates}: {filing.candidate_count ?? "—"} · {filing.admitted_count ?? "—"} {fileMetadataLabels.admitted}</small>}
              {filing.completed_window_count === undefined && Number(filing.window_total ?? 0) > 0 && <small>
                {language === "en" ? "Window" : language === "zh-Hant" ? "視窗" : "窗口"} {filing.window_index ?? 0}/{filing.window_total}
              </small>}
              {detailRows.length > 0 && <details className="filing-progress-details">
                <summary>{fileMetadataLabels.parseDetails}</summary>
                <dl>{detailRows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
              </details>}
              <time dateTime={`PT${Math.max(0, filing.elapsed_seconds ?? 0)}S`}>{formatElapsedTime(filing.elapsed_seconds ?? 0)}</time>
              {filing.source_hash && <code className="filing-source-hash" title={`${fileMetadataLabels.hash}: ${filing.source_hash}`}>{fileMetadataLabels.hash}: {filing.source_hash.slice(0, 12)}</code>}
              {issueCodes.length > 0 && <div className="filing-issue-codes" aria-label={language === "en" ? "Filing issues" : language === "zh-Hant" ? "財報問題代碼" : "财报问题代码"}>
                {issueCodes.map((issueCode) => <code key={issueCode} tabIndex={0}
                  aria-label={language === "en" ? `Filing diagnostic: ${issueCode}` : language === "zh-Hant" ? `財報診斷：${issueCode}` : `财报诊断：${issueCode}`}
                  title={language === "en" ? `Diagnostic code: ${issueCode}. Open financial diagnostics for evidence and recovery details.` : language === "zh-Hant" ? `診斷代碼：${issueCode}。可開啟財報診斷查看證據與恢復詳情。` : `诊断代码：${issueCode}。可打开财报诊断查看证据与恢复详情。`}
                >{issueCode}</code>)}
              </div>}
            </article>;
          }) : <p className="progress-empty-state">{language === "en" ? "Filing details will appear here as documents are processed." : language === "zh-Hant" ? "文件處理進度會顯示在這裡。" : "文件处理进度会显示在这里。"}</p>}
        </div> : <ModelCallProgress calls={job.model_calls ?? []} agentStates={agentStates} language={language}
          runId={job.run_id} loadCallHistory={loadCallHistory}
          cancelling={job.state === "cancelling"} onCancelCall={onCancelCall} />}
      </div>
    </section>
  );
}
