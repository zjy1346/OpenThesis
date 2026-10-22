import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ModelCallProgress } from "./ModelCallProgress";

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
