import { useCallback, useEffect, useRef, useState } from "react";

import {
  bootstrapBackend,
  deleteResearchRun,
  getResearchReport,
  getResearchCallHistory,
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
import { useResearchTasks } from "./useResearchTasks";
import type {
  BootstrapResult,
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

function createClientRequestId(): string {
  return typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `request-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function requestSignature(request: ResearchRequest): string {
  const { client_request_id: _clientRequestId, ...payload } = request;
  return JSON.stringify(payload);
}

function savedPrimaryModel(report: ResearchReport): ResearchRequest["model"] | undefined {
  const configuration = report.reproducibility?.model_configuration;
  if (!configuration || typeof configuration !== "object") return undefined;
  const value = configuration as Record<string, unknown>;
  const configuredModelId = value.configured_model_id;
  const connectionId = value.connection_id;
  const configurationVersion = value.configuration_version ?? value.model_configuration_version;
  if (typeof configuredModelId !== "string" || typeof configurationVersion !== "number") return undefined;
  return {
    configured_model_id: configuredModelId,
    ...(typeof connectionId === "string" ? { connection_id: connectionId } : {}),
    configuration_version: configurationVersion,
    role: "primary",
    ...(typeof value.context_window_hint === "number" || value.context_window_hint === null
      ? { context_window_hint: value.context_window_hint }
      : {}),
  };
}

function firstHistoricalReport(runs: ResearchRunSummary[]): ResearchRunSummary | undefined {
  // A queued run is already durable history, but it has no report yet. Fresh
  // active work must not replace an older readable report on application reload.
  return runs.find((run) => run.status !== "created" && run.status !== "running");
}

const TERMINAL_JOB_STATES = new Set<ResearchJob["state"]>(["completed", "failed", "cancelled"]);

export function isActiveResearchJob(job: ResearchJob | null): job is ResearchJob {
  return Boolean(job && !TERMINAL_JOB_STATES.has(job.state));
}

export function useWorkbenchSession(taskViews?: Parameters<typeof useResearchTasks>[1]) {
  const [bootstrap, setBootstrap] = useState<BootstrapResult | null>(null);
  const bootstrapValue = useRef<BootstrapResult | null>(null);
  const savedPreferences = useRef<Preferences | null>(null);
  const [report, setReport] = useState<ResearchReport | null>(null);
  const [reportLoadState, setReportLoadState] = useState<ReportLoadState>({ status: "idle" });
  const tasks = useResearchTasks(Boolean(bootstrap), taskViews);
  const [error, setError] = useState<WorkbenchError | null>(null);
  const storageMaintenanceRequired = useRef(false);
  const lastRequest = useRef<ResearchRequest | null>(null);
  const reportValue = useRef<ResearchReport | null>(null);
  const selectedReportRun = useRef<string | null>(null);
  const reportReadGeneration = useRef(0);
  const pendingBudgetApprovalRuns = useRef(new Set<string>());
  const initialBootstrap = useRef<Promise<BootstrapResult> | null>(null);
  const failedSubmission = useRef<{ signature: string; clientRequestId: string } | null>(null);
  const refreshedTaskNotifications = useRef(new Set<string>());

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
        const initialRun = firstHistoricalReport(complete.recent_runs);
        if (initialRun
            && selectedReportRun.current === null
            && reportReadGeneration.current === selectionGenerationAtStartup) {
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
    if ([tasks.error, tasks.detailError, tasks.catalog.error].some(isStorageHealthRecoveryRequired)) {
      recordCoreFailure(new Error(STORAGE_HEALTH_RECOVERY_REQUIRED));
    }
  }, [tasks.error, tasks.detailError, tasks.catalog.error, recordCoreFailure]);

  useEffect(() => {
    for (const notification of tasks.notifications) {
      const key = `${notification.job_id}:${notification.revision}`;
      if (refreshedTaskNotifications.current.has(key)) continue;
      refreshedTaskNotifications.current.add(key);
      void bootstrapBackend().then(commitBootstrap).catch((reason) => {
        if (isStorageHealthRecoveryRequired(reason)) recordCoreFailure(reason);
      });
    }
  }, [tasks.notifications, commitBootstrap, recordCoreFailure]);

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
    const signature = requestSignature(request);
    const clientRequestId = request.client_request_id
      ?? (failedSubmission.current?.signature === signature ? failedSubmission.current.clientRequestId : createClientRequestId());
    const submittedRequest = { ...request, client_request_id: clientRequestId };
    lastRequest.current = submittedRequest;
    try {
      const accepted = await startResearch(submittedRequest);
      failedSubmission.current = null;
      tasks.acceptJob(accepted);
      return accepted;
    } catch (reason) {
      if (isStorageHealthRecoveryRequired(reason)) recordCoreFailure(reason);
      else failedSubmission.current = { signature, clientRequestId };
      throw reason;
    }
  };

  const stopResearch = async (jobId: string) => {
    if (storageMaintenanceRequired.current) return;
    await tasks.cancel(jobId);
  };

  const savePreferences = async (preferences: Partial<Preferences>) => {
    const saved = await updatePreferences(preferences);
    savedPreferences.current = saved;
    const current = bootstrapValue.current;
    if (current) commitBootstrap({ ...current, preferences: saved });
    return saved;
  };

  const stopModelCall = async (jobId: string, requestId: string, attemptId: string) => {
    if (storageMaintenanceRequired.current) return;
    await tasks.cancelModel(jobId, requestId, attemptId);
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
    delete retryRequest.client_request_id;
    delete retryRequest.allow_partial_research;
    if (allowPartialResearch) retryRequest.allow_partial_research = true;
    await beginResearch(retryRequest);
  };

  const decideVision = async (jobId: string, revision: number, planId: string, approved: boolean) => {
    if (storageMaintenanceRequired.current) return;
    await tasks.decideVisionUpload(jobId, revision, planId, approved);
  };

  const removeRun = async (run: ResearchRunSummary) => {
    if (storageMaintenanceRequired.current) return;
    setError(null);
    try {
      await deleteResearchRun(run.run_id);
      const nextBootstrap = await bootstrapBackend();
      const currentBootstrap = commitBootstrap(nextBootstrap);
      if (selectedReportRun.current === run.run_id) {
        const nextRun = firstHistoricalReport(currentBootstrap.recent_runs);
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
    if (!report) throw new Error("report is unavailable");
    setError(null);
    tasks.acceptJob(await retryResearchSynthesis(report.run_id, selectedModel));
  };

  const retryModelStages = async () => {
    if (storageMaintenanceRequired.current) return;
    if (!report) throw new Error("report is unavailable");
    setError(null);
    tasks.acceptJob(await retryResearchModelStages(report.run_id));
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
    try {
      commitBootstrap(await bootstrapBackend());
    } catch (reason) {
      if (isStorageHealthRecoveryRequired(reason)) recordCoreFailure(reason);
      // Refreshing navigation metadata is best effort and must not hide the saved report.
    }
  };

  const approveBudgetGaps = async (runId: string, planDigest: string, gapIds: string[], adaptiveChoice?: "keep" | "remove") => {
    if (storageMaintenanceRequired.current) return;
    if (!runId || !planDigest || (gapIds.length === 0 && !adaptiveChoice)) throw new Error("MODEL_BUDGET_APPROVAL_INVALID");
    if (pendingBudgetApprovalRuns.current.has(runId)) throw new Error("MODEL_BUDGET_APPROVAL_IN_PROGRESS");
    pendingBudgetApprovalRuns.current.add(runId);
    try {
      if (adaptiveChoice) await extendResearchBudget(runId, planDigest, gapIds, adaptiveChoice);
      else await extendResearchBudget(runId, planDigest, gapIds);
      tasks.acceptJob(await retryResearchModelStages(runId, undefined, true));
    } catch (reason) {
      if (isStorageHealthRecoveryRequired(reason)) recordCoreFailure(reason);
      await refreshBudgetRunState(runId);
      throw reason;
    } finally {
      pendingBudgetApprovalRuns.current.delete(runId);
    }
  };

  const retryGrowth = async (selectedModel?: ResearchRequest["model"]) => {
    if (storageMaintenanceRequired.current) return;
    const model = selectedModel ?? (report ? savedPrimaryModel(report) : undefined);
    if (!report || !model) {
      throw new Error("model session is unavailable");
    }
    setError(null);
    tasks.acceptJob(await retryResearchGrowth(report.run_id, model));
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
    tasks.acceptJob(await retryResearchStage(report.run_id, target, model, planHash, confirmNewAttempt));
  };

  const revalidateSaved = async () => {
    if (storageMaintenanceRequired.current) return;
    if (!report) throw new Error("report is unavailable");
    setError(null);
    tasks.acceptJob(await revalidateSavedResearch(report.run_id));
  };

  const retryFinancials = async () => {
    if (storageMaintenanceRequired.current) return;
    if (!report) throw new Error("report is unavailable");
    setError(null);
    const started = await startResearchFinancialRetry(report.run_id);
    tasks.acceptJob(started);
  };

  const rebuildFinancials = async () => {
    if (storageMaintenanceRequired.current) return;
    if (!report) throw new Error("report is unavailable");
    setError(null);
    const started = await startResearchFinancialRebuild(report.run_id);
    tasks.acceptJob(started);
  };

  const refreshFinancialReport = async () => {
    if (storageMaintenanceRequired.current) return;
    if (!report) throw new Error("report is unavailable");
    setError(null);
    const next = await refreshFinancialReportBackend(
      report.run_id,
      report.report_language,
    );
    tasks.acceptJob(next);
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
    job: tasks.detail,
    tasks,
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
