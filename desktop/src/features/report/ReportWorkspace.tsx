import { useEffect, useRef, useState, type CSSProperties } from "react";
import {
  Braces,
  Clock3,
  Download,
  Maximize2,
  Minimize2,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { exportResearchReport, getResearchReport } from "../../backend";
import type { ResearchReport } from "../../types";

type ReportCopy = {
  report: string;
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
};

type FocusState = "normal" | "focused" | "closing";
type ExportState = "idle" | "exporting" | "exported" | "failed";

const MIN_ZOOM = 0.9;
const MAX_ZOOM = 1.3;
const ZOOM_STEP = 0.1;
const FOCUS_EXIT_MS = 140;

function nextZoom(current: number, delta: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Number((current + delta).toFixed(1))));
}

export function ReportWorkspace({ report, copy }: { report: ResearchReport; copy: ReportCopy }) {
  const [displayedReport, setDisplayedReport] = useState(report);
  const [zoom, setZoom] = useState(1);
  const [technical, setTechnical] = useState(false);
  const [technicalLoading, setTechnicalLoading] = useState(false);
  const [technicalError, setTechnicalError] = useState("");
  const [exportState, setExportState] = useState<ExportState>("idle");
  const [focusState, setFocusState] = useState<FocusState>("normal");
  const [skipFocusMotion, setSkipFocusMotion] = useState(false);
  const closeTimer = useRef<number | null>(null);

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

  return (
    <article
      className="report-document"
      data-focus={focusState === "normal" ? undefined : focusState}
      data-focus-motion={skipFocusMotion ? "skip" : undefined}
      style={{ "--report-scale": String(zoom) } as CSSProperties}
    >
      <header className="report-meta">
        <div><span className="eyebrow">{displayedReport.ticker}</span><h2>{displayedReport.company_name}</h2></div>
        <div className="report-toolbar" role="toolbar" aria-label={copy.reportTools}>
          <span className="report-label"><Clock3 size={15} /> {copy.report}</span>
          <button type="button" aria-label={copy.zoomOut} title={copy.zoomOut} disabled={zoom <= MIN_ZOOM} onClick={() => setZoom((value) => nextZoom(value, -ZOOM_STEP))}><ZoomOut size={16} /></button>
          <span className="zoom-value" aria-live="polite">{Math.round(zoom * 100)}%</span>
          <button type="button" aria-label={copy.zoomIn} title={copy.zoomIn} disabled={zoom >= MAX_ZOOM} onClick={() => setZoom((value) => nextZoom(value, ZOOM_STEP))}><ZoomIn size={16} /></button>
          <button type="button" aria-label={technical ? copy.hideTechnical : copy.showTechnical} title={technical ? copy.hideTechnical : copy.showTechnical} disabled={technicalLoading} onClick={() => void toggleTechnical()}><Braces size={16} /></button>
          <button type="button" aria-label={copy.exportReport} title={copy.exportReport} disabled={exportState === "exporting"} onClick={() => void exportReport()}><Download size={16} /></button>
          <button type="button" aria-label={focusState === "normal" ? copy.enterFocus : copy.exitFocus} title={focusState === "normal" ? copy.enterFocus : copy.exitFocus} onClick={focusState === "normal" ? () => enterFocus() : () => exitFocus()}>{focusState === "normal" ? <Maximize2 size={16} /> : <Minimize2 size={16} />}</button>
        </div>
      </header>
      {technicalLoading && <span className="report-loading" role="status">{copy.loadingTechnical}</span>}
      {technicalError && <span className="report-loading error" role="alert">{technicalError}</span>}
      {exportState === "exporting" && <span className="report-loading" role="status">{copy.exportingReport}</span>}
      {exportState === "exported" && <span className="report-loading" role="status">{copy.exportedReport}</span>}
      {exportState === "failed" && <span className="report-loading error" role="alert">{copy.exportFailed}</span>}
      <div className="report-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{displayedReport.markdown}</ReactMarkdown></div>
    </article>
  );
}
