import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { exportResearchDiagnostics, getResearchDiagnostics } from "../../backend";
import { ModelCallProgress, ResearchDiagnosticsAction } from "./ModelCallProgress";

vi.mock("../../backend", () => ({ getResearchDiagnostics: vi.fn(), exportResearchDiagnostics: vi.fn() }));

const wireEvidence = import.meta.env.VITE_TEST_WIRE_SNAPSHOT;

describe("model call progress", () => {
  it("keeps hook order stable when call stages appear and disappear", () => {
    const view = render(<ModelCallProgress language="en" cancelling={false} calls={[]} />);
    expect(view.container).toBeEmptyDOMElement();

    view.rerender(<ModelCallProgress language="en" cancelling={false} calls={[{
      request_id: "req-1", attempt_id: "attempt-1", generation: 1, stage_id: "financial-analyst",
      state: "running", elapsed_seconds: 1,
    }]} />);
    expect(screen.getByRole("region", { name: "Research activity" })).toBeVisible();

    view.rerender(<ModelCallProgress language="en" cancelling={false} calls={[]} />);
    expect(view.container).toBeEmptyDOMElement();
  });

  it.runIf(Boolean(wireEvidence))("renders actual status projection without exposing reasoning text", () => {
    const actual = JSON.parse(wireEvidence!);
    expect(actual.cloud_calls).toBe(0);
    expect(actual.provider_calls).toBe(1);
    expect(actual.observed.some((event: { state: string }) => event.state === "reasoning")).toBe(true);
    render(<ModelCallProgress language="en" cancelling={false} calls={actual.model_calls} />);
    expect(screen.getAllByText("Answer saved").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByText(/Usage and call history/));
    expect(screen.getByText("7")).toBeInTheDocument();
    expect(screen.getAllByText("Unknown").length).toBeGreaterThan(0);
    expect(screen.queryByText(/private reasoning/)).not.toBeInTheDocument();
  });

  it("combines agent and current call into one row, folds usage/history, and cancels only that attempt", async () => {
    const cancel = vi.fn();
    const view = render(<ModelCallProgress language="en" cancelling={false} onCancelCall={cancel}
      agentStates={{ "financial-analyst": "running" }} calls={[
        { request_id: "req-old", attempt_id: "old", generation: 1, stage_id: "financial-analyst", state: "saved", elapsed_seconds: 20,
          model_id: "old-model", usage: { input_tokens: 22 } },
        { request_id: "req-current", attempt_id: "current", generation: 2, stage_id: "financial-analyst", state: "reasoning", elapsed_seconds: 45,
          model_id: "vendor/very-long-model-identifier-that-needs-to-wrap", idle_seconds: 0, usage: { input_tokens: 1000 } },
      ]} />);

    const rows = view.container.querySelectorAll(".model-call-stage-row");
    expect(rows).toHaveLength(1);
    expect(within(rows[0] as HTMLElement).getByText("Financial analysis")).toBeInTheDocument();
    expect(within(rows[0] as HTMLElement).getByText("Running")).toBeInTheDocument();
    expect(rows[0].querySelector(".model-call-status[data-state='reasoning']")).toHaveTextContent("Reasoning activity received");
    expect(within(rows[0] as HTMLElement).getByText("vendor/very-long-model-identifier-that-needs-to-wrap")).toBeInTheDocument();
    const details = rows[0].querySelector("details");
    expect(details).not.toHaveAttribute("open");
    fireEvent.click(within(details as HTMLElement).getByText(/Usage and call history/));
    expect(await screen.findByText("1,000")).toBeInTheDocument();
    expect(details).toHaveTextContent("old-model");
    fireEvent.click(screen.getByRole("button", { name: "Stop waiting for this call" }));
    expect(cancel).toHaveBeenCalledWith("req-current", "current");
  });

  it("keeps queued, running, and reasoning visibly distinct", () => {
    const view = render(<ModelCallProgress language="en" cancelling={false} calls={[
      { request_id: "queue", attempt_id: "q", generation: 1, stage_id: "business-analyst", state: "queued", elapsed_seconds: 1 },
      { request_id: "run", attempt_id: "r", generation: 1, stage_id: "financial-analyst", state: "running", elapsed_seconds: 2 },
      { request_id: "reason", attempt_id: "i", generation: 1, stage_id: "skeptical-analyst", state: "reasoning", elapsed_seconds: 3 },
      { request_id: "saved", attempt_id: "s", generation: 1, stage_id: "research-synthesizer", state: "saved", elapsed_seconds: 4 },
    ]} />);
    const currentStates = [...view.container.querySelectorAll(".model-call-status[data-kind='call']")].map((item) => item.textContent);
    expect(currentStates).toEqual(expect.arrayContaining(["Queued", "Running", "Reasoning activity received"]));
    expect(view.container.querySelector(".model-call-stage-row")).toHaveAttribute("data-active", "true");
  });

  it.each([
    ["en", "Reasoning activity received", "Connection remains active", "Connected; waiting for response", "Answer saved", "Stop waiting for this call"],
    ["zh-CN", "收到推理活动", "连接保持中", "已连接，等待响应", "正文已保存", "停止等待本次呼叫"],
    ["zh-Hant", "收到推理活動", "連線保持中", "已連線，等待回應", "正文已儲存", "停止等待本次呼叫"],
  ])("keeps connected and heartbeat states active and cancellable until saved in %s", (language, reasoning, heartbeat, connected, saved, cancelLabel) => {
    const cancel = vi.fn();
    const call = (state: string) => ({
      request_id: "request-heartbeat", attempt_id: "attempt-heartbeat", generation: 1,
      stage_id: "financial-analyst", state, elapsed_seconds: 8,
    });
    const view = render(<ModelCallProgress language={language} cancelling={false} onCancelCall={cancel} calls={[call("reasoning")]} />);
    const row = view.container.querySelector(".model-call-stage-row") as HTMLElement;
    expect(within(row).getByText(reasoning)).toBeVisible();
    fireEvent.click(within(row).getByRole("button", { name: cancelLabel }));
    expect(cancel).toHaveBeenCalledWith("request-heartbeat", "attempt-heartbeat");

    view.rerender(<ModelCallProgress language={language} cancelling={false} onCancelCall={cancel} calls={[call("heartbeat")]} />);
    expect(within(row).getByText(heartbeat)).toBeVisible();
    expect(within(row).queryByText(reasoning)).not.toBeInTheDocument();
    expect(within(row).getByRole("button", { name: cancelLabel })).toBeEnabled();

    view.rerender(<ModelCallProgress language={language} cancelling={false} onCancelCall={cancel} calls={[call("connected")]} />);
    expect(within(row).getByText(connected)).toBeVisible();
    expect(within(row).queryByText(/thinking|思考中|正在思考/i)).not.toBeInTheDocument();
    expect(row).toHaveAttribute("data-active", "true");
    expect(within(row).getByRole("button", { name: cancelLabel })).toBeEnabled();

    view.rerender(<ModelCallProgress language={language} cancelling={false} onCancelCall={cancel} calls={[call("saved")]} />);
    expect(within(row).getByText(saved)).toBeVisible();
    expect(row).toHaveAttribute("data-active", "false");
    expect(within(row).queryByRole("button", { name: cancelLabel })).not.toBeInTheDocument();
  });

  it("shows remaining plan budget separately from local reuse and provider cache, without turning unknown usage into zero", () => {
    render(<ModelCallProgress language="en" cancelling={false} calls={[{
      request_id: "planned-1", attempt_id: "attempt-1", generation: 1, stage_id: "business-analyst", state: "saved", elapsed_seconds: 1,
      call_plan: { max_calls: 4, max_input_estimate: 1_000, max_output_tokens: 2_000 },
      reuse_summary: { local_result_reuses: 2, saved_judgments: 1 },
      usage: { input_tokens: 200, output_tokens: 500, cache_read_input_tokens: 50, cache_write_input_tokens: 0 },
    }]} />);
    const progress = screen.getAllByRole("region", { name: "Research activity" })[0];
    fireEvent.click(within(progress).getByText("All physical calls, including verification and repair"));
    expect(within(progress).getByText("Remaining automatic calls").nextElementSibling).toHaveTextContent("3");
    expect(within(progress).getByText("Remaining input estimate").nextElementSibling).toHaveTextContent("800");
    expect(within(progress).getByText("Remaining output budget").nextElementSibling).toHaveTextContent("1,500");
    expect(within(progress).getByText("Local result reuse (no provider call)").nextElementSibling).toHaveTextContent("2");
    expect(within(progress).getByText("Whole-research provider cache hit rate").nextElementSibling).toHaveTextContent("25%");

    const unknown = render(<ModelCallProgress language="en" cancelling={false} calls={[{
      request_id: "planned-2", attempt_id: "attempt-2", generation: 1, stage_id: "business-analyst", state: "saved", elapsed_seconds: 1,
      call_plan: { max_calls: 4, max_input_estimate: 1_000, max_output_tokens: 2_000 },
      reuse_summary: { local_result_reuses: 0, saved_judgments: 0 },
    }]} />);
    fireEvent.click(within(unknown.container).getByText("All physical calls, including verification and repair"));
    expect(within(unknown.container).getByText("Remaining input estimate").nextElementSibling).toHaveTextContent("Unknown");
    expect(within(unknown.container).getByText("Remaining output budget").nextElementSibling).toHaveTextContent("Unknown");
    expect(within(unknown.container).getByText("Actual cost").nextElementSibling).toHaveTextContent("Unknown");
  });

  it("shows uncapped adaptive remaining work and a non-modal estimate-exceeded status", () => {
    const view = render(<ModelCallProgress language="en" cancelling={false} calls={[{
      request_id: "adaptive-plan", attempt_id: "attempt-adaptive", generation: 1,
      stage_id: "business-analyst", state: "running", elapsed_seconds: 901,
      call_plan: {
        policy: "adaptive", max_calls: null, max_input_estimate: null, max_output_tokens: null,
        remaining_calls: null, remaining_input_tokens: null, remaining_output_tokens: null,
        forecast_exceeded: ["max_calls", "max_input_estimate"],
        initial_forecast: { max_calls: 2, max_input_estimate: 1_000, max_output_tokens: 2_000,
          expected_seconds: 900, estimated_cost: null },
        forecast: { max_calls: 4, max_input_estimate: 2_200, max_output_tokens: 3_000,
          expected_seconds: 1_800 },
      },
    }]} />);

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("The work forecast was exceeded; necessary research continues.");
    expect(status).toHaveTextContent("Call count");
    expect(status).toHaveTextContent("Input tokens");
    expect(status).toHaveTextContent("This is an estimate signal, not a failure or automatic stop.");
    expect(status).not.toHaveAttribute("role", "alert");
    expect(screen.getAllByRole("status")).toHaveLength(1);
    fireEvent.click(screen.getByText("All physical calls, including verification and repair"));
    expect(screen.getByText("Remaining automatic calls").nextElementSibling).toHaveTextContent("No user-set cap");
    expect(screen.getByText("Remaining input estimate").nextElementSibling).toHaveTextContent("No user-set cap");
    expect(screen.getByText("Remaining output budget").nextElementSibling).toHaveTextContent("No user-set cap");
    expect(screen.getByText("Initial calls estimate").nextElementSibling).toHaveTextContent("2");
    expect(screen.getByText("Current calls forecast").nextElementSibling).toHaveTextContent("4");
    expect(screen.getByText("Initial input-token estimate").nextElementSibling).toHaveTextContent("1,000");
    expect(screen.getByText("Current input-token forecast").nextElementSibling).toHaveTextContent("2,200");
  });

  it.each([
    ["en", "Initial estimated duration", "Current estimated duration", "Planning estimate only; not a countdown, hard time cap, or SLA.", "Unknown"],
    ["zh-CN", "初始预计耗时", "当前预计耗时", "仅为规划耗时估算，并非倒计时、硬性时间上限或服务等级承诺。", "未知"],
    ["zh-Hant", "初始預估耗時", "目前預估耗時", "僅為規劃耗時估算，並非倒數計時、硬性時間上限或服務等級承諾。", "未知"],
  ])("shows localized forecast duration separately from any hard time cap (%s)", (language, initialLabel, currentLabel, note, unknown) => {
    const plan = {
      policy: "adaptive", max_calls: null, max_input_estimate: null, max_output_tokens: null,
      remaining_calls: null, remaining_input_tokens: null, remaining_output_tokens: null,
      initial_forecast: { expected_seconds: 900 }, forecast: { expected_seconds: 1_800 },
    };
    const call = {
      request_id: "duration-forecast", attempt_id: "attempt-duration", generation: 1,
      stage_id: "business-analyst", state: "running" as const, elapsed_seconds: 30, call_plan: plan,
    };
    const view = render(<ModelCallProgress language={language} cancelling={false} calls={[call]} />);
    const usage = view.container.querySelector("details.model-call-run-usage")!;
    fireEvent.click(usage.querySelector("summary")!);

    expect(within(usage as HTMLElement).getByText(initialLabel).parentElement as HTMLElement).toHaveTextContent("15:00");
    expect(within(usage as HTMLElement).getByText(currentLabel).parentElement as HTMLElement).toHaveTextContent("30:00");
    expect(usage).toHaveTextContent(note);

    view.rerender(<ModelCallProgress language={language} cancelling={false} calls={[{
      ...call, call_plan: { ...plan, forecast: { expected_seconds: null } },
    }]} />);
    const updatedUsage = view.container.querySelector("details.model-call-run-usage")!;
    expect(within(updatedUsage as HTMLElement).getByText(currentLabel).parentElement as HTMLElement).toHaveTextContent(unknown);
  });

  it.each([
    ["en", "15-minute quality-first reminder", "continues unless you set a limit", "Stop waiting for this call"],
    ["zh-CN", "15分钟质量优先提示", "除非你设置上限，进行中的工作会继续", "停止等待本次呼叫"],
    ["zh-Hant", "15分鐘品質優先提示", "除非你設定上限，進行中的工作會繼續", "停止等待本次呼叫"],
  ])("shows the localized quality-first reminder only when the run plan signals it for %s", (language, reminder, noHardStop, cancelLabel) => {
    const cancel = vi.fn();
    const warnedPlan = { max_calls: 4, max_input_estimate: 1_000, max_output_tokens: 2_000, time_warning: true };
    const view = render(<ModelCallProgress language={language} cancelling={false} onCancelCall={cancel} calls={[{
      request_id: "warning", attempt_id: "attempt-warning", generation: 1, stage_id: "business-analyst", state: "running", elapsed_seconds: 901,
      call_plan: warnedPlan,
    }]} />);
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent(reminder);
    expect(status).toHaveTextContent(noHardStop);
    expect(view.container).not.toHaveTextContent(/time remaining|剩余时间|剩餘時間/);
    const row = view.container.querySelector(".model-call-stage-row") as HTMLElement;
    expect(row).toHaveAttribute("data-active", "true");
    expect(within(row).getByRole("button", { name: cancelLabel })).toBeEnabled();
    expect(cancel).not.toHaveBeenCalled();

    const noWarningPlan = { max_calls: 4, max_input_estimate: 1_000, max_output_tokens: 2_000, time_warning: false };
    view.rerender(<ModelCallProgress language={language} cancelling={false} onCancelCall={cancel} calls={[{
      request_id: "no-warning", attempt_id: "attempt-no-warning", generation: 1, stage_id: "business-analyst", state: "running", elapsed_seconds: 901,
      call_plan: noWarningPlan,
    }]} />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    view.rerender(<ModelCallProgress language={language} cancelling={false} onCancelCall={cancel} calls={[{
      request_id: "unknown-warning", attempt_id: "attempt-unknown-warning", generation: 1,
      stage_id: "business-analyst", state: "running", elapsed_seconds: 901,
      call_plan: { max_calls: 4, max_input_estimate: 1_000, max_output_tokens: 2_000 },
    }]} />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("exports the in-progress run diagnostics only after the user activates the button", async () => {
    const diagnostics = {
      schema: "openthesis.research-diagnostics.v1",
      run: { run_id: "active-run", status: "running", started_at: "now", completed_at: null, wall_elapsed_seconds: 12 },
      totals: { input_tokens: { value: null, known_sum: 0, unknown_attempts: 1 } },
      actual_cost: { amount: null, currency: null, status: "unknown" },
    };
    vi.mocked(getResearchDiagnostics).mockResolvedValue(diagnostics as never);
    vi.mocked(exportResearchDiagnostics).mockResolvedValue(true);
    render(<ResearchDiagnosticsAction runId="active-run" language="en" />);
    expect(getResearchDiagnostics).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Export research diagnostics (cost and time)" }));
    await waitFor(() => expect(getResearchDiagnostics).toHaveBeenCalledWith("active-run"));
    await waitFor(() => expect(exportResearchDiagnostics).toHaveBeenCalledWith(diagnostics));
    expect(screen.getByRole("status")).toHaveTextContent("Diagnostics exported.");
  });

  it.each([
    ["growth-opportunities", "Growth opportunities"],
    ["counter-analysis", "Counter-analysis"],
    ["forecast-scenarios", "Operating scenarios"],
    ["final-synthesis", "Final synthesis"],
  ])("uses the established display role for %s", (stageId, label) => {
    render(<ModelCallProgress language="en" cancelling={false} calls={[{
      request_id: `req-${stageId}`, attempt_id: `attempt-${stageId}`, generation: 1,
      stage_id: stageId, state: "running", elapsed_seconds: 1,
    }]} />);
    expect(screen.getByText(label)).toBeInTheDocument();
  });

  it("localizes traditional Chinese without claiming remote cancellation", () => {
    render(<ModelCallProgress language="zh-Hant" cancelling={false} calls={[{
      request_id: "req", attempt_id: "owner", generation: 1, stage_id: "financial-analyst",
      state: "saved", elapsed_seconds: 45, idle_seconds: 0,
    }]} />);
    expect(screen.getAllByText("正文已儲存").length).toBeGreaterThan(0);
    expect(screen.getByText(/不保證遠端計算或計費已停止/)).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("loads call details only after expansion and fetches later pages explicitly", async () => {
    const pageCall = (index: number) => ({
      request_id: `request-${String(index).padStart(2, "0")}`,
      attempt_id: `attempt-${index}`,
      generation: index,
      stage_id: "business-analyst",
      state: "saved",
      elapsed_seconds: index,
      model_id: `Page model ${index}`,
    });
    const loadHistory = vi.fn(async (_runId: string, _stageId: string, after?: string) => after
      ? { revision: "2:1", calls: [pageCall(11)], next_cursor: null }
      : { revision: "1:1", calls: Array.from({ length: 10 }, (_, index) => pageCall(index + 1)), next_cursor: "cursor-10" });
    const view = render(<ModelCallProgress runId="run-1" loadCallHistory={loadHistory} language="en" cancelling={false}
      calls={[{ ...pageCall(12), model_id: "Summary-only current call", generation: 12 }]} />);
    const details = view.container.querySelector(".model-call-stage-row details.model-call-details") as HTMLDetailsElement;

    expect(details).not.toHaveAttribute("open");
    expect(loadHistory).not.toHaveBeenCalled();
    fireEvent.click(details.querySelector("summary") as HTMLElement);
    await waitFor(() => expect(loadHistory).toHaveBeenCalledWith("run-1", "business-analyst", undefined, 10));
    expect(await screen.findByText(/Page model 10/)).toBeInTheDocument();
    expect(screen.queryByText("Page model 11")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Load more calls" }));
    await waitFor(() => expect(loadHistory).toHaveBeenLastCalledWith("run-1", "business-analyst", "cursor-10", 10));
    expect(await screen.findByText(/Page model 11/)).toBeInTheDocument();
  });

  it("clears loaded pages when the run changes", async () => {
    const loadHistory = vi.fn(async (runId: string) => ({
      revision: runId,
      calls: [{ request_id: `${runId}-request`, attempt_id: runId, generation: 1,
        stage_id: "financial-analyst", state: "saved", elapsed_seconds: 1, model_id: `${runId} history` }],
      next_cursor: null,
    }));
    const props = { language: "en", cancelling: false, calls: [{ request_id: "summary", attempt_id: "s",
      generation: 1, stage_id: "financial-analyst", state: "saved", elapsed_seconds: 1 }] };
    const view = render(<ModelCallProgress {...props} runId="run-1" loadCallHistory={loadHistory} />);
    let details = view.container.querySelector(".model-call-stage-row details.model-call-details") as HTMLDetailsElement;
    fireEvent.click(details.querySelector("summary") as HTMLElement);
    expect(await screen.findByText(/run-1 history/)).toBeInTheDocument();

    view.rerender(<ModelCallProgress {...props} runId="run-2" loadCallHistory={loadHistory} />);
    details = view.container.querySelector(".model-call-stage-row details.model-call-details") as HTMLDetailsElement;
    fireEvent.click(details.querySelector("summary") as HTMLElement);
    expect(await screen.findByText(/run-2 history/)).toBeInTheDocument();
    expect(screen.queryByText(/run-1 history/)).not.toBeInTheDocument();
  });

  it.each([
    ["en", "Material coverage", "Section delivery"],
    ["zh-CN", "材料覆盖", "分章交付"],
    ["zh-Hant", "材料覆蓋", "分章交付"],
  ])("localizes auxiliary request purposes for %s", (language, coverage, section) => {
    const view = render(<ModelCallProgress language={language} cancelling={false} calls={[
      { request_id: "coverage", attempt_id: "c", generation: 1, stage_id: "financial-analyst", state: "saved",
        elapsed_seconds: 1, purpose: "material_coverage" },
      { request_id: "section", attempt_id: "s", generation: 1, stage_id: "research-synthesizer", state: "saved",
        elapsed_seconds: 1, purpose: "section_delivery" },
    ]} />);
    expect(view.container).toHaveTextContent(coverage);
    expect(view.container).toHaveTextContent(section);
  });

  it.each([
    ["en", "UTF-8 planning estimate", "Estimate calibrated from received usage"],
    ["zh-CN", "UTF-8 规划估算", "按已收到的实际用量校准"],
    ["zh-Hant", "UTF-8 規劃估算", "依已收到的實際用量校準"],
  ])("labels planning and observed estimates without claiming a tokenizer for %s", async (language, plan, observed) => {
    const view = render(<ModelCallProgress language={language} cancelling={false} calls={[
      { request_id: "plan", attempt_id: "p", generation: 1, stage_id: "business-analyst", state: "saved", elapsed_seconds: 1,
        diagnostics: { token_counting_method: "utf8_bytes_estimate", input_tokens_estimate: 300, tokenizer: "unavailable" } },
      { request_id: "calibrated", attempt_id: "c", generation: 1, stage_id: "financial-analyst", state: "saved", elapsed_seconds: 1,
        diagnostics: { token_counting_method: "observed_usage_calibrated_estimate", input_tokens_estimate: 200, tokenizer: "unavailable" } },
    ]} />);
    for (const details of view.container.querySelectorAll(".model-call-stage-row details")) {
      fireEvent.click((details as HTMLDetailsElement).querySelector("summary") as HTMLElement);
    }
    await waitFor(() => {
      expect(view.container).toHaveTextContent(plan);
      expect(view.container).toHaveTextContent(observed);
    });
    expect(view.container).not.toHaveTextContent("÷ 3");
  });

  it.each([
    ["en", "Purpose", "Generation", "Input bytes", "Estimated input tokens", "Estimation method", "Tokenizer", "Context window hint", "Reasoning tokens", "Answer/body tokens", "UTF-8 bytes ÷ 3", "Unknown"],
    ["zh-Hant", "用途", "生成", "輸入位元組", "估算輸入 tokens", "估算方式", "Tokenizer", "上下文 tokens 提示", "推理 tokens", "正文 tokens", "UTF-8 位元組 ÷ 3", "未知"],
    ["zh-CN", "用途", "生成", "输入字节", "估算输入 tokens", "估算方式", "Tokenizer", "上下文 tokens 提示", "推理 tokens", "正文 tokens", "UTF-8 字节 ÷ 3", "未知"],
  ])("shows safe usage diagnostics in the folded history for %s", async (language, ...labels) => {
    const [purposeLabel, purposeValue, inputBytes, estimatedInput, estimateMethod, tokenizer, contextHint,
      reasoning, answer, methodValue, unknown] = labels as string[];
    const view = render(<ModelCallProgress language={language} cancelling={false} calls={[{
      request_id: "diagnostic-request", attempt_id: "diagnostic-attempt", generation: 1,
      stage_id: "financial-analyst", state: "saved", elapsed_seconds: 9,
      purpose: "generation",
      usage: { input_tokens: 36, output_tokens: 11 },
      diagnostics: {
        input_bytes: 123456,
        input_tokens_estimate: 41152,
        token_counting_method: "utf8_bytes_div3_estimate",
        tokenizer: "unavailable",
        context_tokens_hint: 128000,
        usage: { reasoning_tokens: 7, answer_tokens: null },
      },
    }]} />);

    const details = view.container.querySelector(".model-call-stage-row details.model-call-details") as HTMLDetailsElement;
    expect(details).not.toHaveAttribute("open");
    fireEvent.click(details.querySelector("summary") as HTMLElement);
    await waitFor(() => expect(details).toHaveAttribute("open"));
    const readValue = (label: string) => {
      const term = [...details.querySelectorAll("dt")].find((item) => item.textContent === label);
      return term?.parentElement?.querySelector("dd")?.textContent;
    };
    expect(details).toHaveTextContent(`${purposeLabel}${purposeValue}`);
    expect(readValue(inputBytes)).toContain("123,456");
    expect(readValue(estimatedInput)).toContain("41,152");
    expect(readValue(estimateMethod)).toBe(methodValue);
    expect(readValue(tokenizer)).toBe("unavailable");
    expect(readValue(contextHint)).toContain("128,000");
    expect(readValue(reasoning)).toBe("7");
    expect(readValue(answer)).toBe(unknown);
    expect(details).not.toHaveTextContent(/private reasoning|answer body contents/i);
  });
});
