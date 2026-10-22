import { useEffect, useMemo, useRef, useState } from "react";
import type { ModelCallHistoryPage, ModelCallStatus } from "../../types";
import { agentDisplayName, formatElapsedTime } from "./researchProgress";

const COPY = {
  en: {
    title: "Research activity", unknown: "Unknown", details: "Usage and call history", attempts: "calls",
    attempt: "Call", history: "Earlier calls", input: "Input", output: "Output",
    verification: "Verification", repair: "Repair", retry: "Retry", cacheRate: "Whole-research provider cache hit rate", uncached: "Uncached input", totalUsage: "All physical calls, including verification and repair", cost: "Actual cost",
    localReuse: "Local result reuse (no provider call)", judgments: "Saved source judgments (not financial facts)", plannedCalls: "Planned automatic call ceiling", plannedInput: "Planning input-token ceiling (estimate)", plannedOutput: "Reserved output-token ceiling",
    purpose: "Purpose", generation: "Generation", semanticVerification: "Semantic verification", claimRepair: "Claim repair", formatRepair: "Format repair",
    inputBytes: "Input bytes", estimatedInputTokens: "Estimated input tokens", estimationMethod: "Estimation method", tokenizer: "Tokenizer", contextHint: "Context window hint",
    reasoningTokens: "Reasoning tokens", answerTokens: "Answer/body tokens", providerTokenizer: "Provider tokenizer", utf8Estimate: "UTF-8 bytes ÷ 3",
    utf8Plan: "UTF-8 planning estimate", observedEstimate: "Estimate calibrated from received usage",
    materialCoverage: "Material coverage", sectionDelivery: "Section delivery",
    read: "Provider cache read", write: "Provider cache write", idle: "Since substantive activity", first: "First answer content",
    validating: "Processing and verifying",
    warning: "No substantive activity for 30 seconds; remote status is unconfirmed. This is not proof of a failure.",
    cancel: "Stop waiting for this call", stopping: "Stopping local wait…", queued: "Queued", running: "Running",
    loadMore: "Load more calls", loadingHistory: "Loading call history…", retryHistory: "Retry loading this page", noHistory: "No call details are available.",
    partial: "Processed; verification incomplete", needsAction: "Action required", reused: "Reused result",
    completed: "Completed", failed: "Failed", cancelled: "Cancelled", waiting: "Waiting",
    states: { queued: "Queued", connecting: "Connecting", waiting_response: "Waiting for response", running: "Running", reasoning: "Reasoning activity received",
      content: "Receiving answer", verifying: "Processing received answer", saved: "Answer saved", cancelled: "Local wait cancelled",
      rate_limited: "Rate limited", rejected: "Rejected by provider", remote_unknown: "Remote outcome unknown", local_failure: "Local processing needs attention" } as Record<string, string>,
    boundary: "Stopping local waiting does not guarantee that remote processing or billing has stopped. No automatic resend.",
  },
  "zh-Hant": {
    title: "研究活動", unknown: "未知", details: "用量與呼叫紀錄", attempts: "次呼叫",
    attempt: "呼叫", history: "先前呼叫", input: "輸入", output: "輸出",
    verification: "核驗", repair: "修復", retry: "重試", cacheRate: "全研究供應商快取命中率", uncached: "未命中輸入", totalUsage: "包含核驗及修復的全部實際呼叫", cost: "實際費用",
    localReuse: "本地結果重用（不呼叫供應商）", judgments: "已存來源核驗（不是財務事實）", plannedCalls: "計畫自動呼叫上限", plannedInput: "規劃輸入Token上限（估算）", plannedOutput: "預留輸出Token上限",
    purpose: "用途", generation: "生成", semanticVerification: "語意核驗", claimRepair: "主張修復", formatRepair: "格式修復",
    inputBytes: "輸入位元組", estimatedInputTokens: "估算輸入 tokens", estimationMethod: "估算方式", tokenizer: "Tokenizer", contextHint: "上下文 tokens 提示",
    reasoningTokens: "推理 tokens", answerTokens: "正文 tokens", providerTokenizer: "供應商 tokenizer", utf8Estimate: "UTF-8 位元組 ÷ 3",
    utf8Plan: "UTF-8 規劃估算", observedEstimate: "依已收到的實際用量校準",
    materialCoverage: "材料覆蓋", sectionDelivery: "分章交付",
    read: "供應商快取讀取", write: "供應商快取寫入", idle: "距上次實質活動", first: "首次正文耗時",
    validating: "整理與核驗中",
    warning: "已超過30秒未收到實質活動，遠端狀態尚無法確認；這不代表呼叫已失敗。",
    cancel: "停止等待本次呼叫", stopping: "正在停止本地等待…", queued: "排隊中", running: "執行中",
    loadMore: "載入更多呼叫", loadingHistory: "正在載入呼叫紀錄…", retryHistory: "重試載入此頁", noHistory: "沒有可用的呼叫詳情。",
    partial: "已處理，核驗尚未完整", needsAction: "需要處理", reused: "已重用結果",
    completed: "已完成", failed: "失敗", cancelled: "已取消", waiting: "等待中",
    states: { queued: "排隊中", connecting: "連線中", waiting_response: "等待遠端回應", running: "執行中", reasoning: "收到推理活動",
      content: "接收正文中", verifying: "正在整理已接收正文", saved: "正文已儲存", cancelled: "已停止本地等待",
      rate_limited: "供應商限流", rejected: "供應商拒絕請求", remote_unknown: "遠端結果未知", local_failure: "本地處理待恢復" } as Record<string, string>,
    boundary: "停止本地等待不保證遠端計算或計費已停止；系統不會自動重送。",
  },
  "zh-CN": {
    title: "研究活动", unknown: "未知", details: "用量与调用记录", attempts: "次调用",
    attempt: "调用", history: "先前调用", input: "输入", output: "输出",
    verification: "核验", repair: "修复", retry: "重试", cacheRate: "全研究供应商缓存命中率", uncached: "未命中输入", totalUsage: "包含核验及修复的全部实际调用", cost: "实际费用",
    localReuse: "本地结果复用（不调用供应商）", judgments: "已存来源核验（不是财务事实）", plannedCalls: "计划自动调用上限", plannedInput: "规划输入Token上限（估算）", plannedOutput: "预留输出Token上限",
    purpose: "用途", generation: "生成", semanticVerification: "语义核验", claimRepair: "主张修复", formatRepair: "格式修复",
    inputBytes: "输入字节", estimatedInputTokens: "估算输入 tokens", estimationMethod: "估算方式", tokenizer: "Tokenizer", contextHint: "上下文 tokens 提示",
    reasoningTokens: "推理 tokens", answerTokens: "正文 tokens", providerTokenizer: "供应商 tokenizer", utf8Estimate: "UTF-8 字节 ÷ 3",
    utf8Plan: "UTF-8 规划估算", observedEstimate: "按已收到的实际用量校准",
    materialCoverage: "材料覆盖", sectionDelivery: "分章交付",
    read: "供应商缓存读取", write: "供应商缓存写入", idle: "距上次实质活动", first: "首次正文耗时",
    validating: "整理与核验中",
    warning: "已超过30秒未收到实质活动，远端状态尚无法确认；这不代表呼叫已失败。",
    cancel: "停止等待本次呼叫", stopping: "正在停止本地等待…", queued: "排队中", running: "运行中",
    loadMore: "加载更多调用", loadingHistory: "正在加载调用记录…", retryHistory: "重试加载此页", noHistory: "没有可用的调用详情。",
    partial: "已处理，核验尚未完整", needsAction: "需要处理", reused: "已复用结果",
    completed: "已完成", failed: "失败", cancelled: "已取消", waiting: "等待中",
    states: { queued: "排队中", connecting: "连接中", waiting_response: "等待远端响应", running: "运行中", reasoning: "收到推理活动",
      content: "接收正文中", verifying: "正在整理已接收正文", saved: "正文已保存", cancelled: "已停止本地等待",
      rate_limited: "供应商限流", rejected: "供应商拒绝请求", remote_unknown: "远端结果未知", local_failure: "本地处理待恢复" } as Record<string, string>,
    boundary: "停止本地等待不保证远端计算或计费已停止；系统不会自动重发。",
  },
};

const ACTIVE = new Set(["queued", "connecting", "waiting_response", "running", "reasoning", "content"]);
const COMPLETED_AGENT_STATES = new Set(["completed", "completed_verified", "completed_partial", "reused_verified"]);
const USAGE_KEYS = ["input_tokens", "output_tokens", "cache_read_input_tokens", "cache_write_input_tokens"] as const;

type StageActivity = { stageId: string; agentState?: string; calls: ModelCallStatus[]; current?: ModelCallStatus };
type HistoryPageState = {
  calls: ModelCallStatus[];
  nextCursor: string | null;
  revision?: string | number;
  loaded: boolean;
  loading: boolean;
  error: boolean;
};

function callIdentity(call: ModelCallStatus): string {
  return `${call.request_id}\u0000${call.attempt_id}`;
}

function uniqueCalls(calls: ModelCallStatus[]): ModelCallStatus[] {
  const seen = new Set<string>();
  return calls.filter((call) => {
    const key = callIdentity(call);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function fallbackHistoryPage(calls: ModelCallStatus[], after?: string): ModelCallHistoryPage {
  const sorted = [...calls].sort((left, right) => left.request_id.localeCompare(right.request_id)
    || left.attempt_id.localeCompare(right.attempt_id));
  const offset = after?.startsWith("fallback:") ? Number(after.slice("fallback:".length)) : 0;
  const start = Number.isInteger(offset) && offset >= 0 ? offset : 0;
  const page = sorted.slice(start, start + 10);
  const end = start + page.length;
  return { revision: "fallback", calls: page, next_cursor: end < sorted.length ? `fallback:${end}` : null };
}

function chooseCurrentCall(calls: ModelCallStatus[]): ModelCallStatus | undefined {
  return calls.reduce<ModelCallStatus | undefined>((current, call) => {
    if (!current) return call;
    const callIsActive = ACTIVE.has(call.state);
    const currentIsActive = ACTIVE.has(current.state);
    if (callIsActive !== currentIsActive) return callIsActive ? call : current;
    if (call.generation !== current.generation) return call.generation > current.generation ? call : current;
    return call.elapsed_seconds >= current.elapsed_seconds ? call : current;
  }, undefined);
}

function agentStateLabel(state: string | undefined, copy: (typeof COPY)["en"]): string | undefined {
  if (!state) return undefined;
  if (state === "queued") return copy.queued;
  if (state === "running" || state === "retrying") return copy.running;
  if (state === "validating") return copy.validating;
  if (state === "completed_partial") return copy.partial;
  if (state === "needs_action") return copy.needsAction;
  if (state === "completed_verified" || state === "completed") return copy.completed;
  if (state === "reused_verified") return copy.reused;
  if (state === "cancelled") return copy.cancelled;
  if (state === "failed" || state === "failed_verification") return copy.failed;
  return copy.waiting;
}

function usageValue(count: number | null | undefined, language: string, unknown: string): string {
  return typeof count === "number" && Number.isFinite(count) && count >= 0
    ? count.toLocaleString(language === "en" ? "en-US" : "zh-CN") : unknown;
}

function elapsedValue(seconds: number | null | undefined, unknown: string): string {
  return typeof seconds === "number" && Number.isFinite(seconds) && seconds >= 0
    ? formatElapsedTime(seconds) : unknown;
}

function purposeLabel(purpose: string, copy: (typeof COPY)["en"]): string {
  const values: Record<string, string> = {
    generation: copy.generation,
    semantic_verification: copy.semanticVerification,
    claim_repair: copy.claimRepair,
    format_repair: copy.formatRepair,
    field_repair: copy.repair,
    output_repair: copy.repair,
    transport_retry: copy.retry,
    material_coverage: copy.materialCoverage,
    section_delivery: copy.sectionDelivery,
  };
  return values[purpose] ?? purpose;
}

function estimationMethodLabel(method: string, copy: (typeof COPY)["en"]): string {
  if (method === "utf8_bytes_div3_estimate") return copy.utf8Estimate;
  if (method === "provider_tokenizer") return copy.providerTokenizer;
  if (method === "utf8_bytes_estimate") return copy.utf8Plan;
  if (method === "observed_usage_calibrated_estimate") return copy.observedEstimate;
  return method;
}

function stageDisplayName(language: string, stageId: string): string {
  const roleByStage: Record<string, string> = {
    "growth-opportunities": "growth-opportunity-analyst",
    "counter-analysis": "skeptical-analyst",
    "forecast-scenarios": "forecast-analyst",
    "final-synthesis": "research-synthesizer",
  };
  return agentDisplayName(language, roleByStage[stageId] ?? stageId);
}

export function ModelCallProgress({ calls, agentStates = {}, language, cancelling, runId, loadCallHistory, onCancelCall }: {
  calls: ModelCallStatus[];
  agentStates?: Record<string, string>;
  language: string;
  cancelling: boolean;
  runId?: string | null;
  loadCallHistory?: (runId: string, stageId: string, after?: string, limit?: number) => Promise<ModelCallHistoryPage>;
  onCancelCall?: (requestId: string, attemptId: string) => void;
}) {
  const [expandedStages, setExpandedStages] = useState<ReadonlySet<string>>(new Set());
  const [historyByStage, setHistoryByStage] = useState<Record<string, HistoryPageState>>({});
  const historyInFlight = useRef(new Set<string>());
  const historyEpoch = useRef(0);
  const currentRunId = runId ?? null;
  const activeStageIds = useRef(new Set<string>());
  const previousRunId = useRef(currentRunId);
  const observedAt = useMemo(() => Date.now(), [calls]);
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!calls.some((call) => ACTIVE.has(call.state))) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [calls]);
  const stages = new Map<string, StageActivity>();
  for (const [stageId, agentState] of Object.entries(agentStates)) {
    stages.set(stageId, { stageId, agentState, calls: [] });
  }
  for (const call of calls) {
    const stage = stages.get(call.stage_id) ?? { stageId: call.stage_id, calls: [] };
    stage.calls.push(call);
    stages.set(call.stage_id, stage);
  }
  const copy = COPY[language === "en" ? "en" : language === "zh-Hant" ? "zh-Hant" : "zh-CN"];
  const rows = [...stages.values()].map((stage) => ({ ...stage, current: chooseCurrentCall(stage.calls) }));
  const stageIdsKey = [...stages.keys()].sort().join("\u0000");
  activeStageIds.current = new Set(stages.keys());
  useEffect(() => {
    if (previousRunId.current === currentRunId) return;
    previousRunId.current = currentRunId;
    historyEpoch.current += 1;
    historyInFlight.current.clear();
    setHistoryByStage({});
    setExpandedStages(new Set());
  }, [currentRunId]);
  useEffect(() => {
    const active = new Set(stageIdsKey ? stageIdsKey.split("\u0000") : []);
    setHistoryByStage((previous) => Object.fromEntries(Object.entries(previous).filter(([stageId]) => active.has(stageId))));
    setExpandedStages((previous) => new Set([...previous].filter((stageId) => active.has(stageId))));
  }, [stageIdsKey]);

  if (stages.size === 0) return null;

  const loadPage = async (stageId: string, after?: string) => {
    const existing = historyByStage[stageId];
    if ((!after && existing?.loaded) || existing?.loading) return;
    const epoch = historyEpoch.current;
    const requestKey = `${epoch}\u0000${stageId}\u0000${after ?? "<first>"}`;
    if (historyInFlight.current.has(requestKey)) return;
    historyInFlight.current.add(requestKey);
    setHistoryByStage((previous) => ({
      ...previous,
      [stageId]: { calls: previous[stageId]?.calls ?? [], nextCursor: previous[stageId]?.nextCursor ?? null,
        revision: previous[stageId]?.revision, loaded: previous[stageId]?.loaded ?? false, loading: true, error: false },
    }));
    try {
      const stage = stages.get(stageId);
      const page = loadCallHistory && currentRunId
        ? await loadCallHistory(currentRunId, stageId, after, 10)
        : fallbackHistoryPage(stage?.calls ?? [], after);
      if (epoch !== historyEpoch.current || !activeStageIds.current.has(stageId)) return;
      if (!page || !Array.isArray(page.calls) || !(typeof page.revision === "string" || typeof page.revision === "number")
          || !(page.next_cursor === null || typeof page.next_cursor === "string")) {
        throw new Error("MODEL_CALL_HISTORY_INVALID");
      }
      setHistoryByStage((previous) => {
        const prior = previous[stageId];
        return {
          ...previous,
          [stageId]: {
            calls: uniqueCalls(after ? [...(prior?.calls ?? []), ...page.calls] : page.calls),
            nextCursor: page.next_cursor,
            revision: page.revision,
            loaded: true,
            loading: false,
            error: false,
          },
        };
      });
    } catch {
      if (epoch !== historyEpoch.current || !activeStageIds.current.has(stageId)) return;
      setHistoryByStage((previous) => ({
        ...previous,
        [stageId]: { calls: previous[stageId]?.calls ?? [], nextCursor: after ?? null,
          revision: previous[stageId]?.revision, loaded: previous[stageId]?.loaded ?? false, loading: false, error: true },
      }));
    } finally {
      historyInFlight.current.delete(requestKey);
    }
  };
  const priority = (stage: StageActivity) => {
    if (stage.current && ACTIVE.has(stage.current.state)) return 0;
    if (["running", "retrying"].includes(stage.agentState ?? "")) return 1;
    if (stage.agentState === "queued") return 2;
    return 3;
  };
  rows.sort((left, right) => priority(left) - priority(right)
    || (right.current?.generation ?? 0) - (left.current?.generation ?? 0)
    || left.stageId.localeCompare(right.stageId));

  const completed = Object.values(agentStates).filter((state) => COMPLETED_AGENT_STATES.has(state)).length;
  const total = Object.keys(agentStates).length;
  const detailLabels = [copy.input, copy.output, copy.read, copy.write];
  const physicalCalls = calls.filter((call) => call.physical_attempt !== false);
  const totalKnown = (key: (typeof USAGE_KEYS)[number]) => physicalCalls.length > 0 && physicalCalls.every((call) =>
    typeof call.usage?.[key] === "number" && Number.isFinite(call.usage[key]) && call.usage[key]! >= 0)
    ? physicalCalls.reduce((sum, call) => sum + call.usage![key]!, 0) : null;
  const input = totalKnown("input_tokens");
  const read = totalKnown("cache_read_input_tokens");
  const validCache = input !== null && read !== null && input > 0 && read <= input;
  const rate = validCache ? new Intl.NumberFormat(language === "en" ? "en-US" : "zh-CN", { style: "percent", maximumFractionDigits: 2 }).format(read / input) : copy.unknown;
  const plan = calls.find((call) => call.call_plan)?.call_plan;
  const reuses = calls.find((call) => call.reuse_summary)?.reuse_summary;
  return <div className="model-call-progress" role="region" aria-label={copy.title}>
    <div className="agent-progress-heading"><span>{copy.title}</span>{total > 0 && <span>{completed}/{total}</span>}</div>
    {physicalCalls.length > 0 && <details className="model-call-details model-call-run-usage">
      <summary>{copy.totalUsage}</summary>
      <dl>
        <div><dt>{copy.input}</dt><dd>{usageValue(input, language, copy.unknown)}</dd></div>
        <div><dt>{copy.uncached}</dt><dd>{usageValue(validCache ? input - read : null, language, copy.unknown)}</dd></div>
        <div><dt>{copy.output}</dt><dd>{usageValue(totalKnown("output_tokens"), language, copy.unknown)}</dd></div>
        <div><dt>{copy.cacheRate}</dt><dd>{rate}</dd></div>
        <div><dt>{copy.cost}</dt><dd>{copy.unknown}</dd></div>
        {reuses && <>
          <div><dt>{copy.localReuse}</dt><dd>{usageValue(reuses.local_result_reuses, language, copy.unknown)}</dd></div>
          <div><dt>{copy.judgments}</dt><dd>{usageValue(reuses.saved_judgments, language, copy.unknown)}</dd></div>
        </>}
        {plan && <>
          <div><dt>{copy.plannedCalls}</dt><dd>{usageValue(plan.max_calls, language, copy.unknown)}</dd></div>
          <div><dt>{copy.plannedInput}</dt><dd>{usageValue(plan.max_input_estimate, language, copy.unknown)}</dd></div>
          <div><dt>{copy.plannedOutput}</dt><dd>{usageValue(plan.max_output_tokens, language, copy.unknown)}</dd></div>
        </>}
      </dl>
    </details>}
    <div className="model-call-stage-list">
      {rows.map((stage) => {
        const call = stage.current;
        const history = historyByStage[stage.stageId];
        const attempts = [...(history?.calls ?? [])].sort((left, right) => right.generation - left.generation
          || right.elapsed_seconds - left.elapsed_seconds);
        return <article className="model-call-stage-row" key={stage.stageId} data-active={Boolean(call && ACTIVE.has(call.state))}>
          <span className={`agent-state-dot agent-state-${stage.agentState ?? call?.state ?? "waiting"}`} aria-hidden="true" />
          <strong className="model-call-stage-name">{stageDisplayName(language, stage.stageId)}</strong>
          <div className="model-call-statuses">
            {stage.agentState && <span className="model-call-status" data-kind="agent">{agentStateLabel(stage.agentState, copy)}</span>}
            {call && <span className="model-call-status" data-kind="call" data-state={call.state}>{copy.states[call.state] ?? copy.unknown}</span>}
            {call?.purpose && <span className="model-call-purpose">{purposeLabel(call.purpose, copy)}</span>}
            <small className="model-call-purpose-counts">{[
              [copy.generation, stage.calls.filter((item) => item.physical_attempt !== false && item.purpose === "generation").length],
              [copy.verification, stage.calls.filter((item) => item.physical_attempt !== false && item.purpose === "semantic_verification").length],
              [copy.repair, stage.calls.filter((item) => item.physical_attempt !== false && ["claim_repair", "field_repair", "output_repair", "format_repair", "section_delivery"].includes(item.purpose ?? "")).length],
            ].map(([label, count]) => `${label} ${count}`).join(" / ")}</small>
          </div>
          {call && <time dateTime={`PT${Math.max(0, call.elapsed_seconds)}S`}>{formatElapsedTime(call.elapsed_seconds + (ACTIVE.has(call.state) ? Math.max(0, now - observedAt) / 1000 : 0))}</time>}
          {call && <span className="model-call-model" title={call.alias || call.model_id || call.configured_model_id || copy.unknown}>
            {call.alias || call.model_id || call.configured_model_id || copy.unknown}
          </span>}
          {call && call.remote_status_unconfirmed && <p className="model-call-warning" role="status">{copy.warning}</p>}
          {call && <div className="model-call-stage-actions">
            {onCancelCall && ACTIVE.has(call.state) && <button className="model-call-stage-cancel" type="button"
              disabled={cancelling || call.cancel_requested}
              onClick={() => onCancelCall(call.request_id, call.attempt_id)}>
              {call.cancel_requested ? copy.stopping : copy.cancel}
            </button>}
            {stage.calls.length > 0 && <details className="model-call-details" key={`${currentRunId ?? "fallback"}:${stage.stageId}`} onToggle={(event) => {
              const open = event.currentTarget.open;
              setExpandedStages((previous) => {
                const next = new Set(previous);
                if (open) next.add(stage.stageId); else next.delete(stage.stageId);
                return next;
              });
              if (open) void loadPage(stage.stageId);
            }}>
              <summary>{copy.details} · {stage.calls.length} {copy.attempts}</summary>
              {expandedStages.has(stage.stageId) && <>
                {attempts.map((attempt, index) => <div className="model-call-attempt" key={`${attempt.request_id}:${attempt.attempt_id}`}>
                <div className="model-call-attempt-heading">
                  <strong>{copy.attempt} {attempts.length - index}</strong>
                  <span>{copy.states[attempt.state] ?? copy.unknown}</span>
                </div>
                <small>{attempt.alias || attempt.model_id || attempt.configured_model_id || copy.unknown} · {copy.idle} {elapsedValue(attempt.idle_seconds, copy.unknown)}</small>
                <dl>
                  {USAGE_KEYS.map((key, usageIndex) => <div key={key}><dt>{detailLabels[usageIndex]}</dt><dd>{usageValue(attempt.usage?.[key], language, copy.unknown)}</dd></div>)}
                  {attempt.purpose !== undefined && <div><dt>{copy.purpose}</dt><dd>{purposeLabel(attempt.purpose, copy)}</dd></div>}
                  {attempt.diagnostics?.input_bytes !== undefined && <div><dt>{copy.inputBytes}</dt><dd>{usageValue(attempt.diagnostics.input_bytes, language, copy.unknown)}</dd></div>}
                  {attempt.diagnostics?.input_tokens_estimate !== undefined && <div><dt>{copy.estimatedInputTokens}</dt><dd>{usageValue(attempt.diagnostics.input_tokens_estimate, language, copy.unknown)}</dd></div>}
                  {attempt.diagnostics?.token_counting_method !== undefined && <div><dt>{copy.estimationMethod}</dt><dd>{attempt.diagnostics.token_counting_method === null
                    ? copy.unknown : estimationMethodLabel(attempt.diagnostics.token_counting_method, copy)}</dd></div>}
                  {attempt.diagnostics?.tokenizer !== undefined && <div><dt>{copy.tokenizer}</dt><dd>{attempt.diagnostics.tokenizer ?? copy.unknown}</dd></div>}
                  {attempt.diagnostics?.context_tokens_hint !== undefined && <div><dt>{copy.contextHint}</dt><dd>{usageValue(attempt.diagnostics.context_tokens_hint, language, copy.unknown)}</dd></div>}
                  {attempt.diagnostics?.usage && "reasoning_tokens" in attempt.diagnostics.usage && <div><dt>{copy.reasoningTokens}</dt><dd>{usageValue(attempt.diagnostics.usage.reasoning_tokens, language, copy.unknown)}</dd></div>}
                  {attempt.diagnostics?.usage && "answer_tokens" in attempt.diagnostics.usage && <div><dt>{copy.answerTokens}</dt><dd>{usageValue(attempt.diagnostics.usage.answer_tokens, language, copy.unknown)}</dd></div>}
                  <div><dt>{copy.first}</dt><dd>{elapsedValue(attempt.first_content_seconds, copy.unknown)}</dd></div>
                  {Object.entries(attempt.phase_seconds ?? {}).filter(([phase]) => phase in copy.states).map(([phase, seconds]) =>
                    <div key={phase}><dt>{copy.states[phase]}</dt><dd>{elapsedValue(seconds, copy.unknown)}</dd></div>)}
                </dl>
                </div>)}
                {history?.loading && <small className="model-call-history-status" role="status">{copy.loadingHistory}</small>}
                {history?.error && <small className="model-call-history-status" role="alert">{copy.noHistory}</small>}
                {history?.loaded && attempts.length === 0 && !history.loading && <small className="model-call-history-status">{copy.noHistory}</small>}
                {history?.error && <button type="button" className="model-call-history-more" disabled={history.loading}
                  onClick={() => void loadPage(stage.stageId, history.nextCursor ?? undefined)}>{copy.retryHistory}</button>}
                {!history?.error && history?.nextCursor && <button type="button" className="model-call-history-more" disabled={history.loading}
                  onClick={() => void loadPage(stage.stageId, history.nextCursor ?? undefined)}>{history.loading ? copy.loadingHistory : copy.loadMore}</button>}
              </>}
            </details>}
          </div>}
        </article>;
      })}
    </div>
    {calls.length > 0 && <small className="model-call-boundary">{copy.boundary}</small>}
  </div>;
}
