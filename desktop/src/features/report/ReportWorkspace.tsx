import { useEffect, useRef, useState, type CSSProperties } from "react";
import {
  Braces,
  Clock3,
  Download,
  Maximize2,
  Minimize2,
  RefreshCw,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { exportResearchReport, getResearchReport } from "../../backend";
import type { ResearchReport } from "../../types";

type ReportCopy = {
  report: string;
  researchRun: string;
  reportDisclaimer: string;
  reportTools: string;
  enterFocus: string;
  exitFocus: string;
  zoomIn: string;
  zoomOut: string;
  showTechnical: string;
  hideTechnical: string;
  loadingTechnical: string;
  technicalFailed: string;
  exportReport: string;
  exportingReport: string;
  exportedReport: string;
  exportFailed: string;
  retrySynthesis: string;
  retryingSynthesis: string;
  retrySynthesisSucceeded: string;
  retrySynthesisFailed: string;
  retryGrowth: string;
  retryingGrowth: string;
  retryGrowthSucceeded: string;
  retryGrowthFailed: string;
  financialEvidenceStatus: string;
  financialComplete: string;
  financialIncomplete: string;
  financialMissingPeriods: string;
  financialAttempts: string;
  financialZeroToken: string;
  retryFinancials: string;
  retryingFinancials: string;
  retryFinancialsSucceeded: string;
  retryFinancialsFailed: string;
  financialRetryPartial: string;
  financialReportRefreshFailed: string;
  refreshFinancialReport: string;
  refreshingFinancialReport: string;
  refreshFinancialReportSucceeded: string;
  refreshFinancialReportFailed: string;
  financialStageStatus: string;
  financialStageDownload: string;
  financialStageValidation: string;
  financialStageProjection: string;
  financialStageRefresh: string;
  financialStageDone: string;
  financialStageFailed: string;
  financialStagePending: string;
  rebuildFinancials: string;
  rebuildFinancialsConfirm: string;
  financialSnapshotStale: string;
  partialReport: string;
  listingCurrency: string;
  reportingCurrency: string;
  sameCurrency: string;
};

type FocusState = "normal" | "focused" | "closing";
type ExportState = "idle" | "exporting" | "exported" | "failed";
type RetryState = "idle" | "retrying" | "succeeded" | "failed";
type RetryTarget = "synthesis" | "growth" | "financials" | "rebuild-financials" | "financial-report";

const MIN_ZOOM = 0.9;
const MAX_ZOOM = 1.3;
const ZOOM_STEP = 0.1;
const FOCUS_EXIT_MS = 140;

function nextZoom(current: number, delta: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Number((current + delta).toFixed(1))));
}

export function stripReportPreamble(markdown: string): string {
  const lines = markdown.split(/\r?\n/);
  if (!["# OpenThesis 长期公司研究", "# OpenThesis Long-term Company Research"].includes(lines[0]?.trim())) {
    return markdown;
  }
  let cursor = 1;
  while (lines[cursor]?.trim() === "") cursor += 1;
  if (/^(研究运行|Research run)[：:]/.test(lines[cursor]?.trim() ?? "")) cursor += 1;
  while (lines[cursor]?.trim() === "") cursor += 1;
  if (/^> (本报告用于研究辅助|This report is research assistance)/.test(lines[cursor]?.trim() ?? "")) cursor += 1;
  while (lines[cursor]?.trim() === "") cursor += 1;
  return lines.slice(cursor).join("\n");
}

export function ReportWorkspace({ report, copy, onRetrySynthesis, onRetryGrowth, onRetryFinancials, onRebuildFinancials, onRefreshFinancialReport }: { report: ResearchReport; copy: ReportCopy; onRetrySynthesis?: () => Promise<void>; onRetryGrowth?: () => Promise<void>; onRetryFinancials?: () => Promise<void>; onRebuildFinancials?: () => Promise<void>; onRefreshFinancialReport?: () => Promise<void> }) {
  const [displayedReport, setDisplayedReport] = useState(report);
  const [zoom, setZoom] = useState(1);
  const [technical, setTechnical] = useState(false);
  const [technicalLoading, setTechnicalLoading] = useState(false);
  const [technicalError, setTechnicalError] = useState("");
  const [exportState, setExportState] = useState<ExportState>("idle");
  const [focusState, setFocusState] = useState<FocusState>("normal");
  const [retryState, setRetryState] = useState<RetryState>("idle");
  const [retryTarget, setRetryTarget] = useState<RetryTarget>("synthesis");
  const [skipFocusMotion, setSkipFocusMotion] = useState(false);
  const closeTimer = useRef<number | null>(null);
  const reportBody = stripReportPreamble(displayedReport.markdown);
  const retryCopy = retryTarget === "financial-report"
    ? {
        retrying: copy.refreshingFinancialReport,
        succeeded: copy.refreshFinancialReportSucceeded,
        failed: copy.refreshFinancialReportFailed,
      }
    : retryTarget === "financials" || retryTarget === "rebuild-financials"
    ? {
        retrying: copy.retryingFinancials,
        succeeded: copy.retryFinancialsSucceeded,
        failed: copy.retryFinancialsFailed,
      }
    : retryTarget === "growth"
    ? {
        retrying: copy.retryingGrowth,
        succeeded: copy.retryGrowthSucceeded,
        failed: copy.retryGrowthFailed,
      }
    : {
      retrying: copy.retryingSynthesis,
        succeeded: copy.retrySynthesisSucceeded,
      failed: copy.retrySynthesisFailed,
    };
  const financialOperation = displayedReport.financial_retry;
  const financialRefreshFailed = Boolean(
    financialOperation?.error?.includes("FILING_REPORT_REFRESH_FAILED"),
  );
  const financialRetryPartial = financialOperation?.status === "partial";
  const financialStage = displayedReport.financial_status?.last_stage ?? "";
  const stageRank = (stage: string): number => ({
    "filing-download": 1,
    "filing-parse": 2,
    "filing-validation": 2,
    "artifact-rebuild": 3,
    "report-refresh": 4,
    completed: 4,
  }[stage] ?? 0);
  const stageText = (rank: number): string => {
    if (financialRefreshFailed && rank === 4) return copy.financialStageFailed;
    if (financialOperation?.status === "succeeded" || stageRank(financialStage) >= rank) return copy.financialStageDone;
    if (retryState === "retrying" && stageRank(financialStage) === rank) return copy.financialStagePending;
    return copy.financialStagePending;
  };
  const financialFailureText = financialRefreshFailed
    ? copy.financialReportRefreshFailed
    : financialRetryPartial
      ? copy.financialRetryPartial
      : retryCopy.failed;
  const reportStatus = retryState === "retrying"
    ? { text: retryCopy.retrying, tone: "normal", role: "status" as const }
    : retryState === "succeeded"
      ? { text: retryCopy.succeeded, tone: "normal", role: "status" as const }
      : retryState === "failed"
        ? { text: financialFailureText, tone: "error", role: "alert" as const }
        : technicalError
          ? { text: technicalError, tone: "error", role: "alert" as const }
          : technicalLoading
            ? { text: copy.loadingTechnical, tone: "normal", role: "status" as const }
            : exportState === "exporting"
              ? { text: copy.exportingReport, tone: "normal", role: "status" as const }
              : exportState === "exported"
                ? { text: copy.exportedReport, tone: "normal", role: "status" as const }
                : exportState === "failed"
                  ? { text: copy.exportFailed, tone: "error", role: "alert" as const }
                  : displayedReport.status === "partial"
                    ? { text: copy.partialReport, tone: "partial", role: "status" as const }
                    : null;
  const compactRunId = displayedReport.run_id.length > 12
    ? `${displayedReport.run_id.slice(0, 12)}…`
    : displayedReport.run_id;

  const cancelPendingClose = () => {
    if (closeTimer.current !== null) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
  };

  useEffect(() => {
    setDisplayedReport(report);
    setTechnical(false);
    setTechnicalError("");
    setExportState("idle");
    setRetryState("idle");
    setRetryTarget("synthesis");
  }, [report]);

  const enterFocus = (withoutMotion = false) => {
    cancelPendingClose();
    setSkipFocusMotion(withoutMotion);
    setFocusState("focused");
  };

  const exitFocus = (withoutMotion = false) => {
    if (focusState === "normal") return;
    cancelPendingClose();
    setSkipFocusMotion(withoutMotion);
    if (withoutMotion) {
      setFocusState("normal");
      return;
    }
    setFocusState("closing");
    closeTimer.current = window.setTimeout(() => {
      setFocusState("normal");
      closeTimer.current = null;
    }, FOCUS_EXIT_MS);
  };

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "F11") {
        event.preventDefault();
        if (focusState === "normal") enterFocus(true); else exitFocus(true);
      } else if (event.key === "Escape" && focusState !== "normal") {
        event.preventDefault();
        exitFocus(true);
      } else if (event.ctrlKey && (event.key === "+" || event.key === "=")) {
        event.preventDefault();
        setZoom((value) => nextZoom(value, ZOOM_STEP));
      } else if (event.ctrlKey && event.key === "-") {
        event.preventDefault();
        setZoom((value) => nextZoom(value, -ZOOM_STEP));
      } else if (event.ctrlKey && event.key === "0") {
        event.preventDefault();
        setZoom(1);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [focusState]);

  useEffect(() => () => cancelPendingClose(), []);

  const toggleTechnical = async () => {
    const next = !technical;
    setTechnicalLoading(true);
    setTechnicalError("");
    try {
      const nextReport = await getResearchReport(report.run_id, report.report_language, next);
      setDisplayedReport(nextReport);
      setTechnical(next);
    } catch {
      setTechnicalError(copy.technicalFailed);
    } finally {
      setTechnicalLoading(false);
    }
  };

  const exportReport = async () => {
    setExportState("exporting");
    try {
      const saved = await exportResearchReport(displayedReport);
      setExportState(saved ? "exported" : "idle");
    } catch {
      setExportState("failed");
    }
  };

  const retryStage = async (target: RetryTarget) => {
    const action = target === "rebuild-financials"
      ? onRebuildFinancials
      : target === "financial-report"
      ? onRefreshFinancialReport
      : target === "financials"
      ? onRetryFinancials
      : target === "growth" ? onRetryGrowth : onRetrySynthesis;
    if (!action) return;
    setRetryTarget(target);
    setRetryState("retrying");
    try {
      await action();
      setRetryState("succeeded");
    } catch {
      setRetryState("failed");
    }
  };

  return (
    <article
      className="report-document"
      data-report-status={displayedReport.status}
      data-focus={focusState === "normal" ? undefined : focusState}
      data-focus-motion={skipFocusMotion ? "skip" : undefined}
      style={{ "--report-scale": String(zoom) } as CSSProperties}
    >
      <header className="report-meta">
        <div className="report-identity">
          <span className="eyebrow">{displayedReport.ticker}</span>
          <h2>{displayedReport.company_name}</h2>
          <div className="report-context">
            <span><Clock3 size={13} />{copy.report}</span>
            <span>{displayedReport.market || "US"}{displayedReport.exchange ? ` · ${displayedReport.exchange}` : ""}</span>
            {displayedReport.listing_currency && displayedReport.reporting_currency && displayedReport.listing_currency !== displayedReport.reporting_currency ? <><span>{copy.listingCurrency}: {displayedReport.listing_currency}</span><span>{copy.reportingCurrency}: {displayedReport.reporting_currency}</span></> : <span>{copy.sameCurrency}: {displayedReport.reporting_currency || displayedReport.listing_currency || "—"}</span>}
            <span className="report-run" title={displayedReport.run_id}>{copy.researchRun}: <code>{compactRunId}</code></span>
            <span>{copy.reportDisclaimer}</span>
          </div>
        </div>
        <div className="report-toolbar" role="toolbar" aria-label={copy.reportTools}>
          {displayedReport.retryable_growth && onRetryGrowth && <button type="button" className="report-retry-button" aria-label={copy.retryGrowth} title={copy.retryGrowth} style={{ width: "auto", minWidth: 34, padding: "0 10px", display: "inline-flex", alignItems: "center", gap: 6 }} disabled={retryState === "retrying"} onClick={() => void retryStage("growth")}><RefreshCw size={16} /><span>{copy.retryGrowth}</span></button>}
          {displayedReport.retryable_synthesis && onRetrySynthesis && <button type="button" className="report-retry-button" aria-label={copy.retrySynthesis} title={copy.retrySynthesis} style={{ width: "auto", minWidth: 34, padding: "0 10px", display: "inline-flex", alignItems: "center", gap: 6 }} disabled={retryState === "retrying"} onClick={() => void retryStage("synthesis")}><RefreshCw size={16} /><span>{copy.retrySynthesis}</span></button>}
          <button type="button" aria-label={copy.zoomOut} title={copy.zoomOut} disabled={zoom <= MIN_ZOOM} onClick={() => setZoom((value) => nextZoom(value, -ZOOM_STEP))}><ZoomOut size={16} /></button>
          <span className="zoom-value" aria-live="polite">{Math.round(zoom * 100)}%</span>
          <button type="button" aria-label={copy.zoomIn} title={copy.zoomIn} disabled={zoom >= MAX_ZOOM} onClick={() => setZoom((value) => nextZoom(value, ZOOM_STEP))}><ZoomIn size={16} /></button>
          <button type="button" aria-label={technical ? copy.hideTechnical : copy.showTechnical} aria-pressed={technical}
            data-active={technical || undefined} title={technical ? copy.hideTechnical : copy.showTechnical}
            disabled={technicalLoading} onClick={() => void toggleTechnical()}>
            <Braces size={16} strokeWidth={technical ? 2.5 : 1.8} />
          </button>
          <button type="button" aria-label={copy.exportReport} title={copy.exportReport} disabled={exportState === "exporting"} onClick={() => void exportReport()}><Download size={16} /></button>
          <button type="button" aria-label={focusState === "normal" ? copy.enterFocus : copy.exitFocus} title={focusState === "normal" ? copy.enterFocus : copy.exitFocus} onClick={focusState === "normal" ? () => enterFocus() : () => exitFocus()}>{focusState === "normal" ? <Maximize2 size={16} /> : <Minimize2 size={16} />}</button>
        </div>
      </header>
      {displayedReport.financial_status && (
        <section className="financial-health" data-state={displayedReport.financial_status.state} aria-label={copy.financialEvidenceStatus}>
          <div className="financial-health-copy">
            <strong>{copy.financialEvidenceStatus}</strong>
            <span>{displayedReport.financial_status.state === "complete" ? copy.financialComplete : copy.financialIncomplete}</span>
            {displayedReport.financial_status.missing_periods.length > 0 && (
              <span>{copy.financialMissingPeriods.replace("{periods}", displayedReport.financial_status.missing_periods.join(", "))}</span>
            )}
            {displayedReport.financial_status.attempt_count > 0 && (
              <span>{copy.financialAttempts.replace("{count}", String(displayedReport.financial_status.attempt_count))}{displayedReport.financial_status.last_stage ? ` · ${displayedReport.financial_status.last_stage}` : ""}</span>
            )}
            <small>{copy.financialZeroToken}</small>
            {displayedReport.financial_status.snapshot_stale && (
              <small className="financial-snapshot-warning">{copy.financialSnapshotStale}</small>
            )}
            {(financialOperation || displayedReport.financial_status.attempt_count > 0) && (
              <div className="financial-stage-status" aria-label={copy.financialStageStatus}>
                <span>{copy.financialStageDownload}: {stageText(1)}</span>
                <span>{copy.financialStageValidation}: {stageText(2)}</span>
                <span>{copy.financialStageProjection}: {stageText(3)}</span>
                <span>{copy.financialStageRefresh}: {stageText(4)}</span>
              </div>
            )}
          </div>
          {displayedReport.financial_status.retryable && (
            <div className="financial-health-actions">
              {financialRefreshFailed && onRefreshFinancialReport && (
                <button type="button" className="report-retry-button" aria-label={copy.refreshFinancialReport} disabled={retryState === "retrying"} onClick={() => void retryStage("financial-report")}>
                  <RefreshCw size={16} /><span>{copy.refreshFinancialReport}</span>
                </button>
              )}
              {onRetryFinancials && !financialRefreshFailed && (
                <button type="button" className="report-retry-button" aria-label={copy.retryFinancials} disabled={retryState === "retrying"} onClick={() => void retryStage("financials")}>
                  <RefreshCw size={16} /><span>{copy.retryFinancials}</span>
                </button>
              )}
              {onRebuildFinancials && (
                <details className="financial-rebuild-advanced">
                  <summary>{copy.rebuildFinancials}</summary>
                  <p>{copy.rebuildFinancialsConfirm}</p>
                  <button type="button" className="report-retry-button report-rebuild-button" aria-label={copy.rebuildFinancials} disabled={retryState === "retrying"} onClick={() => {
                    if (window.confirm(copy.rebuildFinancialsConfirm)) void retryStage("rebuild-financials");
                  }}>
                    <RefreshCw size={16} /><span>{copy.rebuildFinancials}</span>
                  </button>
                </details>
              )}
            </div>
          )}
        </section>
      )}
      {reportStatus && <div className={`report-status report-status-${reportStatus.tone}`} role={reportStatus.role}>{reportStatus.text}</div>}
      <div className="report-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{reportBody}</ReactMarkdown></div>
    </article>
  );
}
