import { useCallback, useEffect, useRef, useState } from "react";

import {
  bootstrapBackend,
  cancelResearch,
  decideVisionUpload,
  deleteResearchRun,
  getResearchReport,
  getResearchStatus,
  openExternalUrl,
  retryResearchModelStages,
  retryResearchSynthesis,
  retryResearchGrowth,
  retryResearchStage,
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
  RecoveryStageTarget,
} from "../types";

export type WorkbenchError =
  | { kind: "core-unavailable"; detail?: string }
  | { kind: "research-failed"; detail: string; code?: string; disclosureUrl?: string }
  | { kind: "report-unavailable"; run_id: string; company_name: string; ticker: string; code: string };

export type ReportLoadState =
  | { status: "idle" }
  | { status: "loading"; run_id: string; company_name: string; ticker: string; preserving_previous: boolean }
  | { status: "loaded"; run_id: string }
  | { status: "failed"; run_id: string; company_name: string; ticker: string; code: string; preserving_previous: boolean };

function reportReadErrorCode(reason: unknown): string {
  const detail = reason instanceof Error ? reason.message : String(reason ?? "");
  if (detail.includes("research run not found")) return "REPORT_NOT_FOUND";
  const code = detail.match(/REPORT_[A-Z_]+|RECOVERY_[A-Z_]+/i)?.[0]?.toUpperCase();
  return code ?? "REPORT_STORAGE_UNAVAILABLE";
}

const TERMINAL_JOB_STATES = new Set<ResearchJob["state"]>(["completed", "failed", "cancelled"]);

export function isActiveResearchJob(job: ResearchJob | null): job is ResearchJob {
  return Boolean(job && !TERMINAL_JOB_STATES.has(job.state));
}

export function useWorkbenchSession() {
  const [bootstrap, setBootstrap] = useState<BootstrapResult | null>(null);
  const [report, setReport] = useState<ResearchReport | null>(null);
  const [reportLoadState, setReportLoadState] = useState<ReportLoadState>({ status: "idle" });
  const [job, setJob] = useState<ResearchJob | null>(null);
  const [error, setError] = useState<WorkbenchError | null>(null);
  const lastRequest = useRef<ResearchRequest | null>(null);
  const reportValue = useRef<ResearchReport | null>(null);
  const selectedReportRun = useRef<string | null>(null);
  const reportReadGeneration = useRef(0);
  const pendingFinancialRetry = useRef<{
    jobId: string;
    resolve: () => void;
    reject: (reason?: unknown) => void;
  } | null>(null);

  const commitReport = useCallback((value: ResearchReport | null) => {
    reportValue.current = value;
    setReport(value);
  }, []);

  const readReportForRun = useCallback(async (
    runId: string,
    language?: Preferences["report_language"],
    identity: { company_name?: string; ticker?: string } = {},
    options: { preserveSameRun?: boolean; onlyIfUnselected?: boolean } = {},
  ): Promise<ResearchReport | null> => {
    if (options.onlyIfUnselected && selectedReportRun.current && selectedReportRun.current !== runId) {
      return null;
    }
    selectedReportRun.current = runId;
    const generation = ++reportReadGeneration.current;
    const previous = reportValue.current;
    const preserving = options.preserveSameRun === true && previous?.run_id === runId;
    if (!preserving) commitReport(null);
    setError(null);
    setReportLoadState({
      status: "loading", run_id: runId,
      company_name: identity.company_name ?? previous?.company_name ?? "",
      ticker: identity.ticker ?? previous?.ticker ?? "",
      preserving_previous: preserving,
    });
    try {
      const loaded = await getResearchReport(runId, language);
      if (generation !== reportReadGeneration.current || selectedReportRun.current !== runId) return null;
      if (loaded.run_id !== runId) throw new Error("REPORT_IDENTITY_MISMATCH");
      commitReport(loaded);
      setReportLoadState({ status: "loaded", run_id: runId });
      setError(null);
      return loaded;
    } catch (reason) {
      if (generation !== reportReadGeneration.current || selectedReportRun.current !== runId) return null;
      const code = reportReadErrorCode(reason);
      if (!preserving) commitReport(null);
      const companyName = identity.company_name ?? previous?.company_name ?? "";
      const ticker = identity.ticker ?? previous?.ticker ?? "";
      setReportLoadState({
        status: "failed", run_id: runId, company_name: companyName, ticker,
        code, preserving_previous: preserving,
      });
      setError({ kind: "report-unavailable", run_id: runId, company_name: companyName, ticker, code });
      return null;
    }
  }, [commitReport]);

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
          const initialRun = value.recent_runs[0];
          await readReportForRun(
            initialRun.run_id,
            value.preferences.report_language,
            { company_name: initialRun.company_name, ticker: initialRun.ticker },
          );
        }
      })
      .catch(() => {
        if (active) setError({ kind: "core-unavailable" });
      });
    return () => { active = false; };
  }, [readReportForRun]);

  useEffect(() => {
    if (!isActiveResearchJob(job)) return;
    let active = true;
    let pollInFlight = false;
    let consecutivePollFailures = 0;
    const poll = window.setInterval(() => {
      if (pollInFlight) return;
      pollInFlight = true;
      void getResearchStatus(job.job_id)
        .then(async (next) => {
          if (!active) return;
          consecutivePollFailures = 0;
          setError((current) => current?.kind === "core-unavailable" ? null : current);
          if (next.state === "completed") {
            window.clearInterval(poll);
            if (!next.run_id) {
              setJob(next);
              finishFinancialJob(next, next.operation_result);
              return;
            }
            try {
              const nextBootstrap = await bootstrapBackend();
              if (!active) return;
              const operationResult = next.operation_result ?? null;
              setJob(next);
              setBootstrap(nextBootstrap);
              const summary = nextBootstrap.recent_runs.find((item) => item.run_id === next.run_id);
              const nextReport = await readReportForRun(
                next.run_id,
                nextBootstrap.preferences.report_language,
                { company_name: summary?.company_name ?? "", ticker: summary?.ticker ?? "" },
                { onlyIfUnselected: true, preserveSameRun: true },
              );
              if (nextReport && operationResult && selectedReportRun.current === next.run_id) {
                commitReport({ ...nextReport, financial_retry: operationResult });
              }
              finishFinancialJob(next, operationResult);
            } catch {
              finishFinancialJob(next);
              if (active) setError({ kind: "core-unavailable" });
            }
          } else if (next.state === "failed") {
            window.clearInterval(poll);
            setJob(next);
            if (next.run_id && next.operation_result) {
              try {
                const nextBootstrap = await bootstrapBackend();
                const summary = nextBootstrap.recent_runs.find((item) => item.run_id === next.run_id);
                setBootstrap(nextBootstrap);
                const nextReport = await readReportForRun(
                  next.run_id,
                  nextBootstrap.preferences.report_language,
                  { company_name: summary?.company_name ?? "", ticker: summary?.ticker ?? "" },
                  { onlyIfUnselected: true, preserveSameRun: true },
                );
                if (active) {
                  if (nextReport && selectedReportRun.current === next.run_id) {
                    commitReport({ ...nextReport, financial_retry: next.operation_result });
                  }
                }
              } catch {
                // The report-read status identifies storage/contract failures.
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
          consecutivePollFailures += 1;
          // A transient sidecar/IPC interruption is not a research failure.
          // Keep polling the durable job and expose the connectivity banner
          // only after several consecutive misses.
          if (consecutivePollFailures >= 3) setError({ kind: "core-unavailable" });
        })
        .finally(() => { pollInFlight = false; });
    }, 350);
    return () => {
      active = false;
      window.clearInterval(poll);
    };
  }, [job?.job_id, bootstrap?.preferences.report_language, commitReport, readReportForRun]);

  const selectRun = async (run: ResearchRunSummary) => {
    await readReportForRun(
      run.run_id,
      bootstrap?.preferences.report_language,
      { company_name: run.company_name, ticker: run.ticker },
    );
  };

  const retryReportRead = async () => {
    if (reportLoadState.status !== "failed") return;
    await readReportForRun(
      reportLoadState.run_id,
      bootstrap?.preferences.report_language,
      { company_name: reportLoadState.company_name, ticker: reportLoadState.ticker },
      { preserveSameRun: true },
    );
  };

  const beginResearch = async (request: ResearchRequest = { mode: "demo" }) => {
    setError(null);
    reportReadGeneration.current += 1;
    selectedReportRun.current = null;
    setReportLoadState({ status: "idle" });
    // A pending run must never leave the previous company's report visible
    // underneath a later failure banner.
    commitReport(null);
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
      if (selectedReportRun.current === run.run_id) {
        const nextRun = nextBootstrap.recent_runs[0];
        if (nextRun) {
          await readReportForRun(
            nextRun.run_id,
            nextBootstrap.preferences.report_language,
            { company_name: nextRun.company_name, ticker: nextRun.ticker },
          );
        } else {
          reportReadGeneration.current += 1;
          selectedReportRun.current = null;
          commitReport(null);
          setReportLoadState({ status: "idle" });
        }
      }
    } catch {
      setError({ kind: "core-unavailable" });
      throw new Error("research deletion failed");
    }
  };

  const retrySynthesis = async (selectedModel?: ResearchRequest["model"]) => {
    const model = selectedModel ?? lastRequest.current?.model;
    if (!report) throw new Error("report is unavailable");
    setError(null);
    const next = await retryResearchSynthesis(report.run_id, model);
    if (selectedReportRun.current === next.run_id) commitReport(next);
    setBootstrap(await bootstrapBackend());
  };

  const retryModelStages = async () => {
    const model = lastRequest.current?.model;
    if (!report) throw new Error("report is unavailable");
    setError(null);
    const next = await retryResearchModelStages(report.run_id, model);
    if (selectedReportRun.current === next.run_id) commitReport(next);
    setBootstrap(await bootstrapBackend());
  };

  const retryGrowth = async () => {
    const model = lastRequest.current?.model;
    if (!report || !model) {
      throw new Error("model session is unavailable");
    }
    setError(null);
    const next = await retryResearchGrowth(report.run_id, model);
    if (selectedReportRun.current === next.run_id) commitReport(next);
    setBootstrap(await bootstrapBackend());
  };

  const retryRecoveryStage = async (
    target: RecoveryStageTarget,
    model: ResearchRequest["model"],
    planHash: string,
  ) => {
    if (!report) throw new Error("report is unavailable");
    if (!model) throw new Error("a model selection is required");
    setError(null);
    const next = await retryResearchStage(report.run_id, target, model, planHash);
    if (selectedReportRun.current === next.run_id) commitReport(next);
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
    if (selectedReportRun.current === next.run_id) commitReport(next);
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
    reportLoadState,
    job,
    error,
    clearError: () => setError(null),
    canRetry: (error?.kind === "core-unavailable" || error?.kind === "research-failed") && lastRequest.current !== null,
    selectRun,
    retryReportRead,
    removeRun,
    beginResearch,
    retryResearch,
    retryModelStages,
    retrySynthesis,
    retryGrowth,
    retryRecoveryStage,
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
