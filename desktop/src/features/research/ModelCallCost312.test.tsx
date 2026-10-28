import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ModelCallProgress } from "./ModelCallProgress";

describe("3.1.2 whole-run call accounting", () => {
  it("does not certify proxy-reported cache as upstream provider cache",()=>{
    const view=render(<ModelCallProgress language="en" cancelling={false} calls={[{request_id:"proxy",attempt_id:"one",stage_id:"financial-analyst",generation:1,state:"saved",elapsed_seconds:1,provider_id:"custom",usage_source:"endpoint_reported_upstream_unverified",usage:{input_tokens:100,cache_read_input_tokens:90,output_tokens:10}}]}/>);
    expect(view.container).toHaveTextContent("upstream calls and cache not independently verified");
    expect(screen.queryByText("90%")).not.toBeInTheDocument();
  });
  it("weights all provider input, including low-hit verification, and separates purposes", () => {
    const view = render(<ModelCallProgress language="en" cancelling={false} calls={[
      { request_id: "gen", attempt_id: "one", stage_id: "financial-analyst", generation: 1,
        state: "saved", elapsed_seconds: 1, purpose: "generation",
        usage: { input_tokens: 1000, cache_read_input_tokens: 900, output_tokens: 20 } },
      { request_id: "verify", attempt_id: "two", stage_id: "financial-analyst", generation: 1,
        state: "saved", elapsed_seconds: 1, purpose: "semantic_verification",
        usage: { input_tokens: 100, cache_read_input_tokens: 0, output_tokens: 20 } },
    ]} />);
    expect(view.container).toHaveTextContent("Generation 1 / Verification 1 / Repair 0");
    expect(screen.getByText("81.82%")).toBeInTheDocument();
    expect(screen.queryByText("90%")).not.toBeInTheDocument();
    expect(screen.getByText("1,100")).toBeInTheDocument();
  });

  it("unknown usage cannot turn into zero cost or a measured cache rate", () => {
    const view = render(<ModelCallProgress language="zh-CN" cancelling={false} calls={[
      { request_id: "unknown", attempt_id: "one", stage_id: "business-analyst", generation: 1,
        state: "remote_unknown", elapsed_seconds: 3, purpose: "generation", usage: { input_tokens: null } },
      { request_id: "denied", attempt_id: "two", stage_id: "business-analyst", generation: 1,
        state: "rejected", elapsed_seconds: 0, purpose: "claim_repair", physical_attempt: false },
    ]} />);
    expect(view.container).toHaveTextContent("生成 1 / 核验 0 / 修复 0");
    expect(screen.queryByText("0%")).not.toBeInTheDocument();
    expect(screen.getAllByText("未知").length).toBeGreaterThan(0);
  });
});
