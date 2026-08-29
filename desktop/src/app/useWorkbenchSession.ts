import { useEffect, useRef, useState } from "react";

import {
  bootstrapBackend,
  cancelResearch,
  decideVisionUpload,
  deleteResearchRun,
  getResearchReport,
  getResearchStatus,
  openExternalUrl,
  retryResearchSynthesis,
  retryResearchGrowth,
  refreshFinancialReport as refreshFinancialReportBackend,
  startResearchFinancialRetry,
  startResearchFinancialRebuild,
  startResearch,
  updatePreferences,
} from "../backend";
import type {
  BootstrapResult,
  FinancialRetryResult,
  Preferences,
  ResearchJob,
  ResearchReport,
  ResearchRequest,
  ResearchRunSummary,
} from "../types";

export type WorkbenchError =
  | { kind: "core-unavailable"; detail?: string }
  | { kind: "research-failed"; detail: string; code?: string; disclosureUrl?: string }
  | { kind: "report-unavailable" };

const TERMINAL_JOB_STATES = new Set<ResearchJob["state"]>(["completed", "failed", "cancelled"]);

export function isActiveResearchJob(job: ResearchJob | null): job is ResearchJob {
  return Boolean(job && !TERMINAL_JOB_STATES.has(job.state));
}

export function useWorkbenchSession() {
  const [bootstrap, setBootstrap] = useState<BootstrapResult | null>(null);
  const [report, setReport] = useState<ResearchReport | null>(null);
  const [job, setJob] = useState<ResearchJob | null>(null);
  const [error, setError] = useState<WorkbenchError | null>(null);
  const lastRequest = useRef<ResearchRequest | null>(null);
  const pendingFinancialRetry = useRef<{
    jobId: string;
    resolve: () => void;
    reject: (reason?: unknown) => void;
  } | null>(null);

  const finishFinancialJob = (job: ResearchJob, result?: FinancialRetryResult | null) => {
    const pending = pendingFinancialRetry.current;
    if (!pending || pending.jobId !== job.job_id) return;
    pendingFinancialRetry.current = null;
    if (job.state === "completed" && result?.status === "succeeded") {
      pending.resolve();
    } else {
      pending.reject(new Error(result?.error || job.message || "Financial evidence refresh was incomplete"));
    }
  };

  useEffect(() => {
    let active = true;
    void bootstrapBackend()
      .then(async (value) => {
        if (!active) return;
        setBootstrap(value);
        if (value.recent_runs[0]) {
          try {
            const initialReport = await getResearchReport(
              value.recent_runs[0].run_id,
              value.preferences.report_language,
            );
            if (active) setReport(initialReport);
          } catch {
            if (active) setError({ kind: "report-unavailable" });
          }
        }
      })
      .catch(() => {
        if (active) setError({ kind: "core-unavailable" });
      });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!isActiveResearchJob(job)) return;
    let active = true;
    const poll = window.setInterval(() => {
      void getResearchStatus(job.job_id)
        .then(async (next) => {
          if (!active) return;
          if (next.state === "completed") {
            window.clearInterval(poll);
            if (!next.run_id) {
              setJob(next);
              finishFinancialJob(next, next.operation_result);
              return;
            }
            try {
              const [nextBootstrap, nextReport] = await Promise.all([
                bootstrapBackend(),
                getResearchReport(next.run_id, bootstrap?.preferences.report_language),
              ]);
              if (!active) return;
              const operationResult = next.operation_result ?? null;
              const refreshedReport = operationResult
                ? { ...nextReport, financial_retry: operationResult }
                : nextReport;
              setJob(next);
              setBootstrap(nextBootstrap);
              setReport(refreshedReport);
              finishFinancialJob(next, operationResult);
            } catch {
              finishFinancialJob(next);
              if (active) setError({ kind: "report-unavailable" });
            }
          } else if (next.state === "failed") {
            window.clearInterval(poll);
            setJob(next);
            if (next.run_id && next.operation_result) {
              try {
                const [nextBootstrap, nextReport] = await Promise.all([
                  bootstrapBackend(),
                  getResearchReport(next.run_id, bootstrap?.preferences.report_language),
                ]);
                if (active) {
                  setBootstrap(nextBootstrap);
                  setReport({ ...nextReport, financial_retry: next.operation_result });
                }
              } catch {
                // Keep the last report visible only as a fallback; the error
                // below explicitly identifies the failed refresh stage.
              }
            }
            finishFinancialJob(next, next.operation_result);
            setError({
              kind: "research-failed",
              detail: next.message,
              code: next.error_code ?? undefined,
              disclosureUrl: next.disclosure_url ?? undefined,
            });
          } else if (next.state === "cancelled") {
            window.clearInterval(poll);
            setJob(next);
            finishFinancialJob(next, next.operation_result);
          } else {
            setJob(next);
          }
        })
        .catch(() => {
          if (!active) return;
          if (pendingFinancialRetry.current?.jobId === job.job_id) {
            const pending = pendingFinancialRetry.current;
            pendingFinancialRetry.current = null;
            pending.reject(new Error("Financial evidence refresh status was unavailable"));
          }
          setError({ kind: "core-unavailable" });
          setJob((current) => current ? { ...current, state: "failed" } : current);
        });
    }, 350);
    return () => {
      active = false;
      window.clearInterval(poll);
    };
  }, [job?.job_id, bootstrap?.preferences.report_language]);

  const selectRun = async (run: ResearchRunSummary) => {
    setError(null);
    try {
      setReport(await getResearchReport(run.run_id, bootstrap?.preferences.report_language));
    } catch {
      setError({ kind: "report-unavailable" });
    }
  };

  const beginResearch = async (request: ResearchRequest = { mode: "demo" }) => {
    setError(null);
    // A pending run must never leave the previous company's report visible
    // underneath a later failure banner.
    setReport(null);
    lastRequest.current = request;
    try {
      setJob(await startResearch(request));
    } catch (reason) {
      setError({
        kind: "core-unavailable",
        detail: reason instanceof Error ? reason.message : undefined,
      });
    }
  };

  const stopResearch = async () => {
    if (!job) return;
    try {
      const next = await cancelResearch(job.job_id);
      setJob(next);
      if (next.state === "cancelled") finishFinancialJob(next, next.operation_result);
    } catch {
      setError({ kind: "core-unavailable" });
    }
  };

  const savePreferences = async (preferences: Partial<Preferences>) => {
    const saved = await updatePreferences(preferences);
    setBootstrap((current) => current ? { ...current, preferences: saved } : current);
    return saved;
  };

  const refreshBootstrap = async () => {
    setError(null);
    try {
      setBootstrap(await bootstrapBackend());
    } catch {
      setError({ kind: "core-unavailable" });
    }
  };

  const retryResearch = async () => {
    if (!lastRequest.current) return;
    await beginResearch(lastRequest.current);
  };

  const decideVision = async (approved: boolean) => {
    if (!job) return;
    try {
      setJob(await decideVisionUpload(job.job_id, approved));
    } catch {
      setError({ kind: "core-unavailable" });
    }
  };

  const removeRun = async (run: ResearchRunSummary) => {
    setError(null);
    try {
      await deleteResearchRun(run.run_id);
      const nextBootstrap = await bootstrapBackend();
      setBootstrap(nextBootstrap);
      if (report?.run_id === run.run_id) {
        const nextRun = nextBootstrap.recent_runs[0];
        setReport(
          nextRun
            ? await getResearchReport(nextRun.run_id, nextBootstrap.preferences.report_language)
            : null,
        );
      }
    } catch {
      setError({ kind: "core-unavailable" });
      throw new Error("research deletion failed");
    }
  };

  const retrySynthesis = async () => {
    const model = lastRequest.current?.model;
    if (!report || !model) {
      throw new Error("model session is unavailable");
    }
    setError(null);
    const next = await retryResearchSynthesis(report.run_id, model);
    setReport(next);
    setBootstrap(await bootstrapBackend());
  };

  const retryGrowth = async () => {
    const model = lastRequest.current?.model;
    if (!report || !model) {
      throw new Error("model session is unavailable");
    }
    setError(null);
    const next = await retryResearchGrowth(report.run_id, model);
    setReport(next);
    setBootstrap(await bootstrapBackend());
  };

  const retryFinancials = async () => {
    if (!report) throw new Error("report is unavailable");
    setError(null);
    const started = await startResearchFinancialRetry(report.run_id);
    return new Promise<void>((resolve, reject) => {
      pendingFinancialRetry.current = { jobId: started.job_id, resolve, reject };
      setJob(started);
      if (TERMINAL_JOB_STATES.has(started.state)) finishFinancialJob(started, started.operation_result);
    });
  };

  const rebuildFinancials = async () => {
    if (!report) throw new Error("report is unavailable");
    setError(null);
    const started = await startResearchFinancialRebuild(report.run_id);
    return new Promise<void>((resolve, reject) => {
      pendingFinancialRetry.current = { jobId: started.job_id, resolve, reject };
      setJob(started);
      if (TERMINAL_JOB_STATES.has(started.state)) finishFinancialJob(started, started.operation_result);
    });
  };

  const refreshFinancialReport = async () => {
    if (!report) throw new Error("report is unavailable");
    setError(null);
    const next = await refreshFinancialReportBackend(
      report.run_id,
      report.report_language,
    );
    setReport(next);
    setBootstrap(await bootstrapBackend());
  };

  const openFailedDisclosure = async () => {
    if (error?.kind !== "research-failed" || !error.disclosureUrl) return;
    try {
      await openExternalUrl(error.disclosureUrl);
    } catch {
      setError({ kind: "core-unavailable" });
    }
  };

  return {
    bootstrap,
    report,
    job,
    error,
    clearError: () => setError(null),
    canRetry: (error?.kind === "core-unavailable" || error?.kind === "research-failed") && lastRequest.current !== null,
    selectRun,
    removeRun,
    beginResearch,
    retryResearch,
    retrySynthesis,
    retryGrowth,
    retryFinancials,
    rebuildFinancials,
    refreshFinancialReport,
    openFailedDisclosure,
    stopResearch,
    decideVisionUpload: decideVision,
    savePreferences,
    refreshBootstrap,
  };
}
