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
  retrySynthesisFailed: string;
};

type FocusState = "normal" | "focused" | "closing";
type ExportState = "idle" | "exporting" | "exported" | "failed";
type RetryState = "idle" | "retrying" | "failed";

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

export function ReportWorkspace({ report, copy, onRetrySynthesis }: { report: ResearchReport; copy: ReportCopy; onRetrySynthesis?: () => Promise<void> }) {
  const [displayedReport, setDisplayedReport] = useState(report);
  const [zoom, setZoom] = useState(1);
  const [technical, setTechnical] = useState(false);
  const [technicalLoading, setTechnicalLoading] = useState(false);
  const [technicalError, setTechnicalError] = useState("");
  const [exportState, setExportState] = useState<ExportState>("idle");
  const [focusState, setFocusState] = useState<FocusState>("normal");
  const [retryState, setRetryState] = useState<RetryState>("idle");
  const [skipFocusMotion, setSkipFocusMotion] = useState(false);
  const closeTimer = useRef<number | null>(null);
  const reportBody = stripReportPreamble(displayedReport.markdown);
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

  const retrySynthesis = async () => {
    if (!onRetrySynthesis) return;
    setRetryState("retrying");
    try {
      await onRetrySynthesis();
      setRetryState("idle");
    } catch {
      setRetryState("failed");
    }
  };

  return (
    <article
      className="report-document"
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
            <span>{displayedReport.market || "US"}{displayedReport.exchange ? ` · ${displayedReport.exchange}` : ""}{displayedReport.listing_currency ? ` · ${displayedReport.listing_currency}` : ""}</span>
            <span className="report-run" title={displayedReport.run_id}>{copy.researchRun}: <code>{compactRunId}</code></span>
            <span>{copy.reportDisclaimer}</span>
          </div>
        </div>
        <div className="report-toolbar" role="toolbar" aria-label={copy.reportTools}>
          {displayedReport.retryable_synthesis && <button type="button" aria-label={copy.retrySynthesis} title={copy.retrySynthesis} disabled={retryState === "retrying"} onClick={() => void retrySynthesis()}><RefreshCw size={16} /></button>}
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
      {technicalLoading && <span className="report-loading" role="status">{copy.loadingTechnical}</span>}
      {technicalError && <span className="report-loading error" role="alert">{technicalError}</span>}
      {exportState === "exporting" && <span className="report-loading" role="status">{copy.exportingReport}</span>}
      {exportState === "exported" && <span className="report-loading" role="status">{copy.exportedReport}</span>}
      {exportState === "failed" && <span className="report-loading error" role="alert">{copy.exportFailed}</span>}
      {retryState === "retrying" && <span className="report-loading" role="status">{copy.retryingSynthesis}</span>}
      {retryState === "failed" && <span className="report-loading error" role="alert">{copy.retrySynthesisFailed}</span>}
      <div className="report-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{reportBody}</ReactMarkdown></div>
    </article>
  );
}
