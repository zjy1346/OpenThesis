import { describe, expect, it } from "vitest";
import type { ConfiguredModelSummary } from "./types";
import { modelEligibility } from "./modelEligibility";

const model: ConfiguredModelSummary = {
  configured_model_id: "m", connection_id: "c", model_id: "remote", alias: "Model",
  free_tier: false, billing_class: "paid", free_source_url: null, free_verified_at: null,
  enabled: true, capabilities: ["text_chat", "structured_json", "vision"], health_status: "error",
  last_discovered_at: null, context_window_hint: null, temperature: null, timeout_seconds: 120,
  configuration_version: 3, connection_enabled: true, credential_available: true,
};
const check = (capability: "text_chat" | "vision", status: "ready" | "error", error_code: string | null = null) => ({
  capability, status, checked_at: 100, error_code, retry_after_seconds: null,
  model_configuration_version: 3, connection_configuration_version: 2, credential_version: 1,
});

describe("capability-local model selection", () => {
  it("keeps text verified when vision alone failed", () => {
    const value = { ...model, capability_checks: [check("text_chat", "ready"), check("vision", "error", "MODEL_CAPABILITY_MISMATCH")] };
    expect(modelEligibility(value, "text_chat").state).toBe("verified");
    expect(modelEligibility(value, "vision").selectable).toBe(false);
  });
  it("keeps temporary failures visible for an explicit retry, honoring cooldown", () => {
    const value = { ...model, capability_checks: [{ ...check("text_chat", "error", "MODEL_RATE_LIMITED"), retry_after_seconds: 30 }] };
    expect(modelEligibility(value, "text_chat", { explicitRetry: true, now: 120 })).toEqual({ selectable: true, canRequest: false, state: "cooldown" });
    expect(modelEligibility(value, "text_chat", { explicitRetry: true, now: 140 }).canRequest).toBe(true);
    expect(modelEligibility(value, "text_chat", { now: 140 }).selectable).toBe(false);
  });
  it("never uses old probe versions or global ready to certify modern untested settings", () => {
    const value = { ...model, health_status: "ready", capability_checks: [{ ...check("text_chat", "ready"), model_configuration_version: 2 }] };
    expect(modelEligibility(value, "text_chat").state).toBe("retest");
    expect(modelEligibility(value, "text_chat", { explicitRetry: true }).selectable).toBe(true);
  });
  it.each(["enabled", "connection_enabled", "credential_available"] as const)("blocks disabled or uncredentialed %s even in explicit retry", (field) => {
    expect(modelEligibility({ ...model, [field]: false }, "text_chat", { explicitRetry: true }).state).toBe("blocked");
  });
  it("keeps unauthorized requests blocked and legacy ready summaries compatible", () => {
    const unauthorized = { ...model, capability_checks: [check("text_chat", "error", "MODEL_UNAUTHORIZED")] };
    expect(modelEligibility(unauthorized, "text_chat", { explicitRetry: true }).state).toBe("blocked");
    expect(modelEligibility({ ...model, health_status: "ready" }, "text_chat").selectable).toBe(true);
  });
});
