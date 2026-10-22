import { useCallback, useEffect, useRef, useState } from "react";

import {
  bootstrapBackend,
  cancelResearch,
  cancelModelCall,
  decideVisionUpload,
  deleteResearchRun,
  getResearchReport,
  getResearchCallHistory,
  getResearchStatus,
  openExternalUrl,
  retryResearchModelStages,
  extendResearchBudget,
  retryResearchSynthesis,
  retryResearchGrowth,
  retryResearchStage,
  revalidateSavedResearch,
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
  | { kind: "storage-recovery-required" }
  | { kind: "research-failed"; detail: string; code?: string; disclosureUrl?: string }
  | { kind: "report-unavailable"; run_id: string; company_name: string; ticker: string; code: string };

const STORAGE_HEALTH_RECOVERY_REQUIRED = "STORAGE_HEALTH_RECOVERY_REQUIRED";

function isStorageHealthRecoveryRequired(reason: unknown): boolean {
  if (reason instanceof Error) return reason.message === STORAGE_HEALTH_RECOVERY_REQUIRED;
  if (typeof reason === "string") return reason === STORAGE_HEALTH_RECOVERY_REQUIRED;
  if (!reason || typeof reason !== "object") return false;
  const value = reason as { code?: unknown; message?: unknown };
  return value.code === STORAGE_HEALTH_RECOVERY_REQUIRED || value.message === STORAGE_HEALTH_RECOVERY_REQUIRED;
}

export type ReportLoadState =
  | { status: "idle" }
  | { status: "loading"; run_id: string; company_name: string; ticker: string; preserving_previous: boolean }
  | { status: "loaded"; run_id: string }
  | { status: "failed"; run_id: string; company_name: string; ticker: string; code: string; preserving_previous: boolean };

function reportReadErrorCode(reason: unknown): string {
  if (isStorageHealthRecoveryRequired(reason)) return STORAGE_HEALTH_RECOVERY_REQUIRED;
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
  const bootstrapValue = useRef<BootstrapResult | null>(null);
  const savedPreferences = useRef<Preferences | null>(null);
  const [report, setReport] = useState<ResearchReport | null>(null);
  const [reportLoadState, setReportLoadState] = useState<ReportLoadState>({ status: "idle" });
  const [job, setJob] = useState<ResearchJob | null>(null);
  const [error, setError] = useState<WorkbenchError | null>(null);
  const storageMaintenanceRequired = useRef(false);
  const lastRequest = useRef<ResearchRequest | null>(null);
  const reportValue = useRef<ResearchReport | null>(null);
  const selectedReportRun = useRef<string | null>(null);
  const reportReadGeneration = useRef(0);
  const pendingBudgetApprovalRuns = useRef(new Set<string>());
  const initialBootstrap = useRef<Promise<BootstrapResult> | null>(null);
  const pendingFinancialRetry = useRef<{
    jobId: string;
    resolve: () => void;
    reject: (reason?: unknown) => void;
  } | null>(null);

  const commitReport = useCallback((value: ResearchReport | null) => {
    reportValue.current = value;
    setReport(value);
  }, []);

  const commitBootstrap = useCallback((value: BootstrapResult): BootstrapResult => {
    const next = savedPreferences.current
      ? { ...value, preferences: savedPreferences.current }
      : value;
    bootstrapValue.current = next;
    setBootstrap(next);
    return next;
  }, []);

  const recordCoreFailure = useCallback((reason: unknown) => {
    if (isStorageHealthRecoveryRequired(reason)) {
      storageMaintenanceRequired.current = true;
      setError({ kind: "storage-recovery-required" });
    } else if (!storageMaintenanceRequired.current) {
      setError({ kind: "core-unavailable" });
    }
  }, []);

  const readReportForRun = useCallback(async (
    runId: string,
    language?: Preferences["report_language"],
    identity: { company_name?: string; ticker?: string } = {},
    options: { preserveSameRun?: boolean; onlyIfUnselected?: boolean } = {},
  ): Promise<ResearchReport | null> => {
    if (storageMaintenanceRequired.current) return null;
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
      const storageFailure = code === STORAGE_HEALTH_RECOVERY_REQUIRED;
      if (storageFailure && previous !== null) commitReport(previous);
      else if (!preserving) commitReport(null);
      const companyName = identity.company_name ?? previous?.company_name ?? "";
      const ticker = identity.ticker ?? previous?.ticker ?? "";
      setReportLoadState({
        status: "failed", run_id: runId, company_name: companyName, ticker,
        code, preserving_previous: preserving || (storageFailure && previous !== null),
      });
      if (storageFailure) recordCoreFailure(reason);
      else setError({ kind: "report-unavailable", run_id: runId, company_name: companyName, ticker, code });
      return null;
    }
  }, [commitReport, recordCoreFailure]);

  const finishFinancialJob = (job: ResearchJob, result?: FinancialRetryResult | null) => {
    const pending = pendingFinancialRetry.current;
    if (!pending || pending.jobId !== job.job_id) return;
    pendingFinancialRetry.current = null;
    if (job.state === "completed" && (result?.status === "succeeded"
      || (result?.status === "partial" && result.research_can_continue === true))) {
      pending.resolve();
    } else {
      pending.reject(new Error(result?.error || job.message || "Financial evidence refresh was incomplete"));
    }
  };

  useEffect(() => {
    let active = true;
    const selectionGenerationAtStartup = reportReadGeneration.current;
    initialBootstrap.current ??= bootstrapBackend(true);
    void initialBootstrap.current
      .then(async (value) => {
        if (!active) return;
        commitBootstrap(value);
        // Navigation/settings can render before history and report projection.
        const complete = await bootstrapBackend();
        if (!active) return;
        const currentBootstrap = commitBootstrap(complete);
        if (complete.recent_runs[0]
            && selectedReportRun.current === null
            && reportReadGeneration.current === selectionGenerationAtStartup) {
          const initialRun = complete.recent_runs[0];
          await readReportForRun(
            initialRun.run_id,
            bootstrapValue.current?.preferences.report_language ?? currentBootstrap.preferences.report_language,
            { company_name: initialRun.company_name, ticker: initialRun.ticker },
          );
        }
      })
      .catch((reason) => {
        if (active) recordCoreFailure(reason);
      });
    return () => { active = false; };
  }, [commitBootstrap, readReportForRun, recordCoreFailure]);

  useEffect(() => {
    if (!isActiveResearchJob(job)) return;
    let active = true;
    let pollInFlight = false;
    let consecutivePollFailures = 0;
    let unchangedPolls = 0;
    let lastRevision: string | undefined;
    let stopped = false;
    let poll: number;
    const pollOnce = () => {
      if (pollInFlight) return;
      pollInFlight = true;
      void getResearchStatus(job.job_id)
        .then(async (next) => {
          if (!active) return;
          consecutivePollFailures = 0;
          const changed = !next.status_revision || next.status_revision !== lastRevision;
          unchangedPolls = changed ? 0 : unchangedPolls + 1;
          lastRevision = next.status_revision;
          setError((current) => current?.kind === "core-unavailable" ? null : current);
          if (next.state === "completed") {
            stopped = true;
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
              const currentBootstrap = commitBootstrap(nextBootstrap);
              const summary = currentBootstrap.recent_runs.find((item) => item.run_id === next.run_id);
              const nextReport = await readReportForRun(
                next.run_id,
                currentBootstrap.preferences.report_language,
                { company_name: summary?.company_name ?? "", ticker: summary?.ticker ?? "" },
                { onlyIfUnselected: true, preserveSameRun: true },
              );
              if (nextReport && operationResult && selectedReportRun.current === next.run_id) {
                commitReport({ ...nextReport, financial_retry: operationResult });
              }
              finishFinancialJob(next, operationResult);
            } catch (reason) {
              finishFinancialJob(next);
              if (active) recordCoreFailure(reason);
            }
          } else if (next.state === "failed") {
            stopped = true;
            setJob(next);
            if (next.run_id && next.operation_result) {
              try {
                const nextBootstrap = await bootstrapBackend();
                const currentBootstrap = commitBootstrap(nextBootstrap);
                const summary = currentBootstrap.recent_runs.find((item) => item.run_id === next.run_id);
                const nextReport = await readReportForRun(
                  next.run_id,
                  currentBootstrap.preferences.report_language,
                  { company_name: summary?.company_name ?? "", ticker: summary?.ticker ?? "" },
                  { onlyIfUnselected: true, preserveSameRun: true },
                );
                if (active) {
                  if (nextReport && selectedReportRun.current === next.run_id) {
                    commitReport({ ...nextReport, financial_retry: next.operation_result });
                  }
                }
              } catch (reason) {
                // The report-read status identifies storage/contract failures.
                if (isStorageHealthRecoveryRequired(reason)) recordCoreFailure(reason);
              }
            }
            finishFinancialJob(next, next.operation_result);
            if (!storageMaintenanceRequired.current) {
              setError({
                kind: "research-failed",
                detail: next.message,
                code: next.error_code ?? undefined,
                disclosureUrl: next.disclosure_url ?? undefined,
              });
            }
          } else if (next.state === "cancelled") {
            stopped = true;
            setJob(next);
            finishFinancialJob(next, next.operation_result);
          } else {
            if (changed) setJob(next);
          }
        })
        .catch((reason) => {
          if (!active) return;
          if (isStorageHealthRecoveryRequired(reason)) {
            stopped = true;
            recordCoreFailure(reason);
            return;
          }
          consecutivePollFailures += 1;
          // A transient sidecar/IPC interruption is not a research failure.
          // Keep polling the durable job and expose the connectivity banner
          // only after several consecutive misses.
          if (consecutivePollFailures >= 3) setError({ kind: "core-unavailable" });
        })
        .finally(() => {
          pollInFlight = false;
          if (active && !stopped) poll = window.setTimeout(pollOnce,
            consecutivePollFailures > 0 || unchangedPolls >= 3 ? 3000 : unchangedPolls > 0 ? 2000 : 1000);
        });
    };
    poll = window.setTimeout(pollOnce, 1000);
    return () => {
      active = false;
      window.clearTimeout(poll);
    };
  }, [job?.job_id, bootstrap?.preferences.report_language, commitBootstrap, commitReport, readReportForRun, recordCoreFailure]);

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
    if (storageMaintenanceRequired.current) return;
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
      if (isStorageHealthRecoveryRequired(reason)) recordCoreFailure(reason);
      else setError({ kind: "core-unavailable", detail: reason instanceof Error ? reason.message : undefined });
    }
  };

  const stopResearch = async () => {
    if (!job) return;
    try {
      const next = await cancelResearch(job.job_id);
      setJob(next);
      if (next.state === "cancelled") finishFinancialJob(next, next.operation_result);
    } catch (reason) {
      recordCoreFailure(reason);
    }
  };

  const savePreferences = async (preferences: Partial<Preferences>) => {
    const saved = await updatePreferences(preferences);
    savedPreferences.current = saved;
    const current = bootstrapValue.current;
    if (current) commitBootstrap({ ...current, preferences: saved });
    return saved;
  };

  const stopModelCall = async (requestId: string, attemptId: string) => {
    if (!job) return;
    try {
      const next = await cancelModelCall(job.job_id, requestId, attemptId);
      setJob((current) => current?.job_id === next.job_id ? next : current);
    } catch (reason) {
      recordCoreFailure(reason);
    }
  };

  const refreshBootstrap = async () => {
    if (storageMaintenanceRequired.current) return;
    setError(null);
    try {
      commitBootstrap(await bootstrapBackend());
    } catch (reason) {
      recordCoreFailure(reason);
    }
  };

  const retryResearch = async (allowPartialResearch = false) => {
    if (!lastRequest.current) return;
    const retryRequest = { ...lastRequest.current };
    delete retryRequest.allow_partial_research;
    if (allowPartialResearch) retryRequest.allow_partial_research = true;
    await beginResearch(retryRequest);
  };

  const decideVision = async (approved: boolean) => {
    if (storageMaintenanceRequired.current) return;
    if (!job) return;
    try {
      setJob(await decideVisionUpload(job.job_id, approved));
    } catch (reason) {
      recordCoreFailure(reason);
    }
  };

  const removeRun = async (run: ResearchRunSummary) => {
    if (storageMaintenanceRequired.current) return;
    setError(null);
    try {
      await deleteResearchRun(run.run_id);
      const nextBootstrap = await bootstrapBackend();
      const currentBootstrap = commitBootstrap(nextBootstrap);
      if (selectedReportRun.current === run.run_id) {
        const nextRun = currentBootstrap.recent_runs[0];
        if (nextRun) {
          await readReportForRun(
            nextRun.run_id,
            currentBootstrap.preferences.report_language,
            { company_name: nextRun.company_name, ticker: nextRun.ticker },
          );
        } else {
          reportReadGeneration.current += 1;
          selectedReportRun.current = null;
          commitReport(null);
          setReportLoadState({ status: "idle" });
        }
      }
    } catch (reason) {
      recordCoreFailure(reason);
      throw new Error("research deletion failed");
    }
  };

  const retrySynthesis = async (selectedModel?: ResearchRequest["model"]) => {
    if (storageMaintenanceRequired.current) return;
    const model = selectedModel ?? lastRequest.current?.model;
    if (!report) throw new Error("report is unavailable");
    setError(null);
    const next = await retryResearchSynthesis(report.run_id, model);
    if (selectedReportRun.current === next.run_id) commitReport(next);
    commitBootstrap(await bootstrapBackend());
  };

  const retryModelStages = async () => {
    if (storageMaintenanceRequired.current) return;
    const model = lastRequest.current?.model;
    if (!report) throw new Error("report is unavailable");
    setError(null);
    const next = await retryResearchModelStages(report.run_id, model);
    if (selectedReportRun.current === next.run_id) commitReport(next);
    commitBootstrap(await bootstrapBackend());
  };

  const loadResearchCallHistory = useCallback((runId: string, stageId: string, after?: string, limit = 10) =>
    getResearchCallHistory(runId, stageId, after, limit), []);

  const refreshBudgetRunState = async (runId: string) => {
    try {
      const refreshedReport = await getResearchReport(runId, bootstrap?.preferences.report_language);
      if (selectedReportRun.current === runId || !selectedReportRun.current) {
        selectedReportRun.current = runId;
        const current = reportValue.current;
        commitReport(current?.run_id === runId
          ? { ...refreshedReport, markdown: current.markdown, html: current.html }
          : refreshedReport);
        setReportLoadState({ status: "loaded", run_id: runId });
      }
    } catch (reason) {
      if (isStorageHealthRecoveryRequired(reason)) recordCoreFailure(reason);
      // Keep the existing report body visible if a refresh is unavailable.
    }
    if (job?.run_id === runId) {
      try {
        const refreshedJob = await getResearchStatus(job.job_id);
        setJob((current) => current?.job_id === refreshedJob.job_id ? refreshedJob : current);
      } catch (reason) {
        if (isStorageHealthRecoveryRequired(reason)) recordCoreFailure(reason);
        // The prior job snapshot remains usable; a later poll can refresh it.
      }
    }
    try {
      commitBootstrap(await bootstrapBackend());
    } catch (reason) {
      if (isStorageHealthRecoveryRequired(reason)) recordCoreFailure(reason);
      // Refreshing navigation metadata is best effort and must not hide the saved report.
    }
  };

  const approveBudgetGaps = async (runId: string, planDigest: string, gapIds: string[]) => {
    if (storageMaintenanceRequired.current) return;
    if (!runId || !planDigest || gapIds.length === 0) throw new Error("MODEL_BUDGET_APPROVAL_INVALID");
    if (pendingBudgetApprovalRuns.current.has(runId)) throw new Error("MODEL_BUDGET_APPROVAL_IN_PROGRESS");
    pendingBudgetApprovalRuns.current.add(runId);
    try {
      await extendResearchBudget(runId, planDigest, gapIds);
      const updated = await retryResearchModelStages(runId, undefined, true);
      if (selectedReportRun.current === updated.run_id || !selectedReportRun.current) {
        selectedReportRun.current = updated.run_id;
        commitReport(updated);
        setReportLoadState({ status: "loaded", run_id: updated.run_id });
      }
      try {
        commitBootstrap(await bootstrapBackend());
      } catch {
        // The recovered report is already committed; bootstrap refresh is best effort.
      }
    } catch (reason) {
      if (isStorageHealthRecoveryRequired(reason)) recordCoreFailure(reason);
      await refreshBudgetRunState(runId);
      throw reason;
    } finally {
      pendingBudgetApprovalRuns.current.delete(runId);
    }
  };

  const retryGrowth = async () => {
    if (storageMaintenanceRequired.current) return;
    const model = lastRequest.current?.model;
    if (!report || !model) {
      throw new Error("model session is unavailable");
    }
    setError(null);
    const next = await retryResearchGrowth(report.run_id, model);
    if (selectedReportRun.current === next.run_id) commitReport(next);
    commitBootstrap(await bootstrapBackend());
  };

  const retryRecoveryStage = async (
    target: RecoveryStageTarget,
    model: ResearchRequest["model"],
    planHash: string,
    confirmNewAttempt = false,
  ) => {
    if (storageMaintenanceRequired.current) return;
    if (!report) throw new Error("report is unavailable");
    if (!model) throw new Error("a model selection is required");
    setError(null);
    const next = await retryResearchStage(report.run_id, target, model, planHash, confirmNewAttempt);
    if (selectedReportRun.current === next.run_id) commitReport(next);
    commitBootstrap(await bootstrapBackend());
  };

  const revalidateSaved = async () => {
    if (storageMaintenanceRequired.current) return;
    if (!report) throw new Error("report is unavailable");
    setError(null);
    const next = await revalidateSavedResearch(report.run_id);
    if (selectedReportRun.current === next.run_id) commitReport(next);
    commitBootstrap(await bootstrapBackend());
  };

  const retryFinancials = async () => {
    if (storageMaintenanceRequired.current) return;
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
    if (storageMaintenanceRequired.current) return;
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
    if (storageMaintenanceRequired.current) return;
    if (!report) throw new Error("report is unavailable");
    setError(null);
    const next = await refreshFinancialReportBackend(
      report.run_id,
      report.report_language,
    );
    if (selectedReportRun.current === next.run_id) commitReport(next);
    commitBootstrap(await bootstrapBackend());
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
    approveBudgetGaps,
    loadResearchCallHistory,
    retrySynthesis,
    retryGrowth,
    retryRecoveryStage,
    revalidateSaved,
    retryFinancials,
    rebuildFinancials,
    refreshFinancialReport,
    openFailedDisclosure,
    stopResearch,
    stopModelCall,
    decideVisionUpload: decideVision,
    savePreferences,
    refreshBootstrap,
  };
}
