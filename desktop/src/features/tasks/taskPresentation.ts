import type { Language, ModelCallPlan, ResearchJob, ResearchTaskState, ResearchTaskSummary } from "../../types";
import { progressStageCopy } from "../research/researchProgress";
import { getResearchTaskCopy } from "./researchTasksCopy";

export const TASK_FILTERS = ["active", "attention", "ended", "all"] as const;
export type TaskFilter = typeof TASK_FILTERS[number];
export const TASK_FILTER_STATES: Record<TaskFilter, ResearchTaskState[] | undefined> = {
  active: ["running", "queued", "cancelling"],
  attention: ["waiting_input", "interrupted"],
  ended: ["completed", "failed", "cancelled"],
  all: undefined,
};

export function matchesTaskFilter(state: ResearchTaskState, filter: TaskFilter): boolean {
  return TASK_FILTER_STATES[filter]?.includes(state) ?? true;
}

export function taskFilterFor(state: ResearchTaskState): TaskFilter {
  return TASK_FILTERS.find((filter) => matchesTaskFilter(state, filter)) ?? "all";
}

export function taskStatus(task: Pick<ResearchJob, "state" | "research_complete" | "result_access">, language: Language): string {
  const copy = getResearchTaskCopy(language);
  if (task.state === "completed" && (task.research_complete !== true || task.result_access !== "saved")) {
    return task.result_access === "saved" ? copy.partialResult : copy.attemptEnded;
  }
  return copy.states[task.state];
}

export function taskStage(task: Pick<ResearchJob, "state" | "stage" | "execution_draining">, language: Language): string {
  return progressStageCopy(language, task.execution_draining && task.state !== "cancelling"
    ? "task-finalizing" : task.state === "running" ? task.stage : task.state).title;
}

export function taskOperation(operation: string | undefined, language: Language): string {
  const labels = getResearchTaskCopy(language).operations;
  const value = (operation ?? "research").trim().toLowerCase();
  return labels[value] ?? labels.research;
}

export function reportAction(task: Pick<ResearchTaskSummary, "run_id" | "result_access" | "research_complete"> | Pick<ResearchJob, "run_id" | "result_access" | "research_complete">, language: Language): string | null {
  if (!task.run_id) return null;
  const copy = getResearchTaskCopy(language);
  if (task.result_access === "saved") return task.research_complete === true ? copy.viewReport : copy.viewSaved;
  return task.result_access === "recovery" ? copy.reportRecovery : null;
}

export function hasBudgetApproval(plan?: ModelCallPlan | null): boolean {
  return Boolean(plan?.plan_digest && (plan.pending_budget_gaps?.some((gap) => gap.gap_id)
    || plan.migration_choice_required
    || (plan.limit_source === "legacy_default" && plan.adaptive_resume_available)));
}

const CONFIGURATION_FAILURE = /MODEL_(?:CONFIGURATION_CHANGED|CONFIGURATION_(?:INVALID|ERROR)|CONNECTION_AUTH_PAUSED|CREDENTIAL_(?:MISSING|INVALID)|UNAUTHORIZED|AUTHENTICATION_ERROR|GATEWAY_CONFIGURATION_ERROR|DISABLED)|NEEDS_CONFIGURATION/i;
const REMOTE_UNKNOWN = /MODEL_REMOTE_RESULT_UNKNOWN|REMOTE_UNKNOWN|REMOTE_STATUS_UNCONFIRMED|OUTCOME_UNKNOWN/i;
const CODE_REASON = /^[A-Z][A-Z0-9_.-]{2,}$/;
const OFFICIAL_DISCLOSURE_DOMAINS = ["sec.gov", "cninfo.com.cn", "hkexnews.hk"] as const;

function officialDisclosureUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase();
    const allowed = OFFICIAL_DISCLOSURE_DOMAINS.some((domain) => host === domain || host.endsWith(`.${domain}`));
    return url.protocol === "https:" && !url.port && !url.username && !url.password && allowed ? url.href : null;
  } catch {
    return null;
  }
}

export function taskCapabilities(job: ResearchJob, language: Language) {
  const copy = getResearchTaskCopy(language);
  const codes = [job.blocked_reason, job.error_code, job.recovery_outcome?.diagnostic_code]
    .filter((value): value is string => Boolean(value?.trim())).map((value) => value.trim());
  const code = [...codes].reverse().find((value) => CODE_REASON.test(value.trim())) ?? "";
  const remoteUnknown = codes.some((candidate) => REMOTE_UNKNOWN.test(candidate))
    || Boolean(job.model_calls?.some((call) => call.state === "remote_unknown" || call.remote_status_unconfirmed));
  const waiting = matchesTaskFilter(job.state, "attention");
  const ended = matchesTaskFilter(job.state, "ended");
  const reason = remoteUnknown ? copy.remoteUnknown : [...codes].reverse().map((value) => copy.reasons[value.toUpperCase()]).find(Boolean)
    ?? codes.find((value) => !CODE_REASON.test(value)) ?? copy.genericReason;
  const needsRequest = codes.some((value) => /LEGACY_REQUEST_UNAVAILABLE|TASK_REQUEST_UNAVAILABLE|RESEARCH_TASK_NOT_REQUEUEABLE/.test(value));
  const approval = !waiting || remoteUnknown || needsRequest ? null
    : job.vision_approval_pending ? "vision"
    : codes.includes("TASK_VISION_POLICY_CHANGED") ? "vision-settings"
    : codes.some((value) => CONFIGURATION_FAILURE.test(value)) ? "model"
    : hasBudgetApproval(job.call_plan) ? "budget"
    : codes.some((value) => /MODEL_(?:RUN_.*BUDGET|RUN_PLAN_REQUIRES_APPROVAL)/.test(value)) ? null
    : "continue";
  return {
    waiting, ended, remoteUnknown, code, reason, approval,
    report: reportAction(job, language),
    disclosure: officialDisclosureUrl(job.disclosure_url),
    cancel: waiting || job.state === "queued" || (job.state === "running" && !job.execution_draining && !job.cancel_requested),
  };
}
