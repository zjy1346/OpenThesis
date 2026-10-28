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
type FilingProgress = NonNullable<ResearchJob["filing_states"]>[string];

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
const COMPLETED_FILING_STATUSES = new Set(["validated", "parsed", "cache-hit"]);
const SETTLED_FILING_STATUSES = new Set([
  ...COMPLETED_FILING_STATUSES, "blocked", "failed", "cancelled", "complete", "usable", "partial",
  "none", "historical", "not-applicable", "needs-action", "needs_action",
]);
const ACTIVE_FILING_STATUSES = new Set([
  "cache-check", "indexing", "local-parsing", "local-validating", "window-checkpointed", "window-cache-hit",
  "window-failed", "page-parsing", "page-checkpointed", "page-cache-hit", "page-failed", "cloud-awaiting-approval",
  "cloud-processing", "canonical-compiling", "processing",
]);

function filingActivityTime(filing: FilingProgress): number {
  const timestamp = filing.last_activity_at ? Date.parse(filing.last_activity_at) : Number.NaN;
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function latestFiling(items: FilingProgress[]): FilingProgress | null {
  return items.reduce<FilingProgress | null>((latest, item) =>
    latest === null || filingActivityTime(item) >= filingActivityTime(latest) ? item : latest, null);
}

function currentFiling(filings: FilingProgress[]): FilingProgress | null {
  const active = filings.filter((item) => !SETTLED_FILING_STATUSES.has(item.status)
    && (item.waiting_type != null || ACTIVE_FILING_STATUSES.has(item.status)));
  if (active.length) return latestFiling(active);
  const incompleteCandidate = filings.filter((item) => !SETTLED_FILING_STATUSES.has(item.status)
    && item.candidate_page_count !== undefined
    && (item.completed_page_count ?? 0) < item.candidate_page_count);
  return latestFiling(incompleteCandidate);
}

function filingKind(filing: FilingProgress): string | null {
  const period = filing.fiscal_period?.trim().toUpperCase();
  if (!period) return null;
  if (period === "FY" || period.startsWith("FY") || period.includes("ANNUAL")) return "annual";
  if (/^(Q[1-4]|QUARTER)/.test(period)) return "quarterly";
  if (/^(H[12]|6M|INTERIM|HALF)/.test(period)) return "interim";
  if (/^(AS[_ -]?OF|POINT[_ -]?IN[_ -]?TIME)/.test(period)) return "as-of";
  return null;
}

function hasModelResearchActivity(job: ResearchJob): boolean {
  if (RESEARCH_STAGES.has(job.stage ?? "") || Object.keys(job.agent_states ?? {}).length > 0) return true;
  return (job.model_calls ?? []).some((call) => RESEARCH_AGENT_IDS.has(call.stage_id)
    || call.stage_id.startsWith("research-"));
}

function isFilingProgressStalled(job: ResearchJob): boolean {
  if (!["filing-parse", "filing-window", "filing-page"].includes(job.stage ?? "")) return false;
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

function estimateRemainingRange(job: ResearchJob, stalled: boolean, activeFiling: FilingProgress | null): RemainingRange | null {
  if (job.state !== "running" || job.vision_approval_pending || job.stage === "vision-approval" || stalled) return null;
  const filings = Object.values(job.filing_states ?? {});
  let remaining: number | null = null;
  if (["filing-parse", "filing-page"].includes(job.stage ?? "")
    && activeFiling?.waiting_type === "native_parse"
    && typeof activeFiling.parser_backend === "string" && activeFiling.parser_backend.length > 0
    && Number.isInteger(activeFiling.parsed_page_count) && (activeFiling.parsed_page_count ?? 0) >= 3
    && Number.isFinite(activeFiling.parsing_active_seconds) && (activeFiling.parsing_active_seconds ?? 0) > 0
    && Number.isInteger(activeFiling.candidate_page_count)
    && Number.isInteger(activeFiling.completed_page_count)) {
    const pagesRemaining = Math.max(0, activeFiling.candidate_page_count! - activeFiling.completed_page_count!);
    if (pagesRemaining > 0) {
      remaining = (activeFiling.parsing_active_seconds! / activeFiling.parsed_page_count!) * pagesRemaining;
    }
  } else if (job.stage === "filing-validation" && activeFiling?.waiting_type === "local_validation") {
    const kind = filingKind(activeFiling);
    const samples = kind ? filings.filter((item) => item.status === "validated" && filingKind(item) === kind
      && Number.isFinite(item.elapsed_seconds) && (item.elapsed_seconds ?? 0) > 0) : [];
    if (kind && samples.length >= 2) {
      const pendingSameKind = filings.filter((item) => filingKind(item) === kind
        && !SETTLED_FILING_STATUSES.has(item.status)).length;
      if (pendingSameKind > 0) {
        remaining = samples.reduce((sum, item) => sum + (item.elapsed_seconds ?? 0), 0) / samples.length * pendingSameKind;
      }
    }
  }
  if (remaining === null || !Number.isFinite(remaining) || remaining <= 0) return null;
  const bounded = Math.min(7_200, Math.max(5, remaining));
  return {
    lower: Math.max(5, Math.floor(bounded * 0.6)),
    upper: Math.min(7_200, Math.max(10, Math.ceil(bounded * 1.5))),
  };
}

function formatProductTime(timestamp: string, language: string): string {
  const date = new Date(timestamp);
  if (!Number.isFinite(date.getTime())) return timestamp;
  const locale = language === "en" ? "en-US" : language === "zh-Hant" ? "zh-Hant" : "zh-CN";
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function waitingTypeLabel(type: string, language: string): string {
  const labels: Record<string, readonly [string, string, string]> = {
    native_parse: ["本地解析中", "Native parsing", "本地解析中"],
    local_validation: ["本地结果校验中", "Local validation", "本地結果驗證中"],
    cache_reuse: ["复用缓存结果", "Reusing cached output", "重用快取結果"],
    external: ["等待外部处理", "Waiting on external work", "等待外部處理"],
  };
  const value = labels[type];
  return value ? value[language === "en" ? 1 : language === "zh-Hant" ? 2 : 0] : type;
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
  const filingStates = Object.values(job.filing_states ?? {});
  const activeFiling = currentFiling(filingStates);
  const progressStalled = isFilingProgressStalled(job);
  const remaining = estimateRemainingRange(job, progressStalled, activeFiling);
  const remainingLabel = language === "en"
    ? "Estimated remaining"
    : language === "zh-Hant" ? "預計剩餘範圍" : "预计剩余范围";
  const estimatingCopy = language === "en" ? "Estimating from completed work…"
    : language === "zh-Hant" ? "正在依已完成工作估算……" : "正在依已完成工作估算……";
  const stalledCopy = language === "en"
    ? "Deep parsing is still active. The remaining-time range will return after the next filing completes."
    : language === "zh-Hant"
      ? "仍在深度解析財報。下一份財報完成後，系統會重新估算剩餘時間。"
      : "仍在深度解析财报。下一份财报完成后，系统会重新估算剩余时间。";
  const timeWarningCopy = language === "en"
    ? "15-minute quality-first reminder: review research progress before adding automatic work. This is a reminder only; active calls continue, and 20 minutes is not a hard stop."
    : language === "zh-Hant"
      ? "15分鐘品質優先提示：請先檢視研究進度，再決定是否增加自動工作。這只是提醒，不會取消在途呼叫；20分鐘不是硬性終止時限。"
      : "15分钟质量优先提示：请先检查研究进度，再决定是否增加自动工作。这只是提醒，不会取消在途调用；20分钟不是硬性终止时限。";
  const hasCallTimeWarning = (job.model_calls ?? []).some((call) => call.call_plan?.time_warning === true);
  const showTopLevelTimeWarning = job.call_plan?.time_warning === true
    && (activeTab === "preparation" || !hasCallTimeWarning);
  const externalStage = ["filing-download", "vision-approval", "vision-processing", "comparison"].includes(stage ?? "");
  const sinceBackend = Math.max(0, elapsed - backendElapsed);
  const activeElapsed = Math.max(0, Math.floor((job.engine_active_seconds ?? backendElapsed) + (externalStage ? 0 : sinceBackend)));
  const externalElapsed = Math.max(0, Math.floor((job.external_wait_seconds ?? 0) + (externalStage ? sinceBackend : 0)));
  const timingLabels = language === "en"
    ? { total: "Total", active: "Engine", external: "External" }
    : language === "zh-Hant"
      ? { total: "總計", active: "引擎", external: "外部等待" }
      : { total: "总计", active: "引擎", external: "外部等待" };
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
      "page-checkpointed": ["页面已保存，继续识别", "Page saved; parsing continues", "頁面已儲存，繼續識別"],
      "page-parsing": ["正在识别当前页", "Parsing current page", "正在識別目前頁面"],
      "page-cache-hit": ["已复用页面，等待汇总校验", "Page reused; validation pending", "已重用頁面，等待彙總驗證"],
      "page-failed": ["页面需要修复", "Page needs repair", "頁面需要修復"],
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
      pages: "Total pages", nativePages: "Native-text pages", indexedPages: "Indexed pages", pagesComplete: "Candidate pages complete", windowsComplete: "Legacy windows complete",
      parsedPages: "Newly parsed pages", activeParseTime: "Active parsing time", parserBackend: "Parser backend",
      parseElapsed: "Parse time", attempts: "Attempts", cacheReused: "Cached result reused", yes: "Yes", no: "No",
      recognizedStatements: "Recognized statements", extractedPages: "Pages with extracted facts", failureStage: "Failure stage", rootError: "Root error code" }
    : language === "zh-Hant"
      ? { roles: "角色", candidates: "候選", admitted: "已納入", hash: "來源雜湊", parseDetails: "解析詳情",
        pages: "總頁數", nativePages: "含原生文字頁數", indexedPages: "已建立索引頁數", pagesComplete: "已完成候選頁", windowsComplete: "舊版視窗完成數",
        parsedPages: "本輪新解析頁數", activeParseTime: "本輪解析耗時", parserBackend: "解析後端",
        parseElapsed: "解析耗時", attempts: "嘗試次數", cacheReused: "重用快取結果", yes: "是", no: "否",
        recognizedStatements: "已識別報表", extractedPages: "含擷取事實的頁面", failureStage: "失敗階段", rootError: "根錯誤代碼" }
      : { roles: "角色", candidates: "候选", admitted: "已纳入", hash: "来源哈希", parseDetails: "解析详情",
        pages: "总页数", nativePages: "含原生文本页数", indexedPages: "已建立索引页数", pagesComplete: "已完成候选页", windowsComplete: "旧版窗口完成数",
        parsedPages: "本轮新解析页数", activeParseTime: "本轮解析耗时", parserBackend: "解析后端",
        parseElapsed: "解析耗时", attempts: "尝试次数", cacheReused: "复用缓存结果", yes: "是", no: "否",
        recognizedStatements: "已识别报表", extractedPages: "含提取事实的页面", failureStage: "失败阶段", rootError: "根错误代码" };
  const roleNames: Record<string, readonly [string, string, string]> = {
    target: ["目标公司", "Target", "目標公司"],
    comparator: ["对照公司", "Comparator", "比較公司"],
    historical: ["历史资料", "Historical", "歷史資料"],
  };
  const roleLabel = (role: string) => roleNames[role]?.[language === "en" ? 1 : language === "zh-Hant" ? 2 : 0] ?? role;
  const completedFilingCount = filingStates.filter((filing) => COMPLETED_FILING_STATUSES.has(filing.status)
    || filing.processing_state === "completed" || FINAL_FILING_STATUSES.has(filing.status)).length;
  const countLocale = language === "en" ? "en-US" : language === "zh-Hant" ? "zh-Hant" : "zh-CN";
  const formatCount = (value: number) => value.toLocaleString(countLocale);
  const filingCountLabel = language === "en" ? "Filings processed:"
    : language === "zh-Hant" ? "已處理財報：" : "已处理财报：";
  const candidatePageLabel = language === "en" ? "Current file candidate pages:"
    : language === "zh-Hant" ? "目前文件候選頁：" : "当前文件候选页：";
  const currentCandidateProgress = activeFiling
    && Number.isInteger(activeFiling.completed_page_count)
    && Number.isInteger(activeFiling.candidate_page_count)
    ? `${formatCount(activeFiling.completed_page_count!)} / ${formatCount(activeFiling.candidate_page_count!)}`
    : null;
  const productActivity = filingStates.filter((filing) => filing.last_product_at || filing.waiting_type
    || (Number.isInteger(filing.current_page) && (filing.current_page ?? 0) > 0));
  const activityLabels = language === "en"
    ? { summary: "Filing activity details", lastProduct: "Last locatable product", waiting: "Current work state", page: "Current PDF page" }
    : language === "zh-Hant"
      ? { summary: "財報活動詳情", lastProduct: "最近可定位產物", waiting: "目前工作狀態", page: "目前 PDF 頁碼" }
      : { summary: "财报活动详情", lastProduct: "最近可定位产物", waiting: "当前工作状态", page: "当前 PDF 页码" };
  const showEstimating = ["filing-parse", "filing-page", "filing-validation"].includes(stage ?? "")
    && !remaining && !progressStalled && job.state === "running" && !job.vision_approval_pending;
  return (
    <section className="research-progress" data-state={job.state}>
      <div className="research-progress-summary">
        <div className="research-progress-header">
          <div className="research-stage-copy" aria-live="polite">
            <strong>{stageCopy.title}</strong>
            <span>“{stageCopy.note}”</span>
            {stageDetail && <small>{stageDetail}</small>}
            {filingStates.length > 0 && <small>
              {filingCountLabel} {formatCount(completedFilingCount)} / {formatCount(filingStates.length)}
              {currentCandidateProgress && <> · {candidatePageLabel} {currentCandidateProgress}</>}
            </small>}
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
        {showEstimating && <small className="research-remaining">{estimatingCopy}</small>}
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
        {showTopLevelTimeWarning && <div className="research-stalled" role="status">{timeWarningCopy}</div>}
        {productActivity.length > 0 && <details className="filing-progress-details">
          <summary>{activityLabels.summary}</summary>
          <dl>{productActivity.map((filing) => <div key={filing.filing_id}>
            <dt>{filing.label}</dt>
            <dd>
              {filing.last_product_at && <div>{activityLabels.lastProduct}: <time dateTime={filing.last_product_at}>
                {formatProductTime(filing.last_product_at, language)}</time></div>}
              {filing.waiting_type && <div>{activityLabels.waiting}: {waitingTypeLabel(filing.waiting_type, language)}</div>}
              {Number.isInteger(filing.current_page) && (filing.current_page ?? 0) > 0
                && <div>{activityLabels.page}: {formatCount(filing.current_page!)}</div>}
            </dd>
          </div>)}</dl>
        </details>}
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
            if (filing.completed_page_count !== undefined || filing.candidate_page_count !== undefined) {
              detailRows.push([fileMetadataLabels.pagesComplete, `${numberText(filing.completed_page_count ?? 0)} / ${numberText(filing.candidate_page_count ?? 0)}`]);
            }
            if (filing.completed_window_count !== undefined || filing.window_total !== undefined) {
              detailRows.push([fileMetadataLabels.windowsComplete, `${numberText(filing.completed_window_count ?? 0)} / ${numberText(filing.window_total ?? 0)}`]);
            }
            if (filing.parsed_page_count !== undefined) detailRows.push([fileMetadataLabels.parsedPages, numberText(filing.parsed_page_count)]);
            if (filing.parsing_active_seconds !== undefined) detailRows.push([fileMetadataLabels.activeParseTime, formatElapsedTime(filing.parsing_active_seconds)]);
            if (filing.parser_backend) detailRows.push([fileMetadataLabels.parserBackend, filing.parser_backend]);
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
              {filing.completed_page_count !== undefined && filing.candidate_page_count !== undefined && <small>
                {fileMetadataLabels.pagesComplete}: {numberText(filing.completed_page_count)} / {numberText(filing.candidate_page_count)}
              </small>}
              {filing.completed_page_count === undefined && filing.completed_window_count === undefined && Number(filing.window_total ?? 0) > 0 && <small>
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
