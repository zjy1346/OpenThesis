import type { ConfiguredModelSummary } from "./types";

type Capability = "text_chat" | "structured_json" | "vision";
export type ModelEligibility = {
  selectable: boolean;
  canRequest: boolean;
  state: "verified" | "retest" | "blocked" | "cooldown";
};

const TEMPORARY = /^MODEL_(?:RATE_LIMITED|HTTP_429|TIMEOUT|NETWORK_ERROR|TRANSPORT_ERROR|HTTP_5\d\d)$/;
const NO_AUTH = /^MODEL_(?:CREDENTIAL_MISSING|CREDENTIAL_INVALID|UNAUTHORIZED|AUTHENTICATION_ERROR|HTTP_401|HTTP_403|DISABLED|CONFIGURATION_ERROR)$/;

/** A vision failure is not evidence that text chat failed. The host exposes
 * only probes matching current configuration/credential versions. */
export function modelEligibility(model: ConfiguredModelSummary, capability: Capability,
  { explicitRetry = false, now = Date.now() / 1000 } = {}): ModelEligibility {
  const blocked: ModelEligibility = { selectable: false, canRequest: false, state: "blocked" };
  if (!model.enabled || model.connection_enabled === false || model.credential_available === false) return blocked;
  const declared = model.capabilities.includes(capability)
    || (capability === "text_chat" && model.capabilities.includes("structured_json"));
  if (!declared) return blocked;
  const check = model.capability_checks?.find((item) => item.capability === capability
    && item.model_configuration_version === model.configuration_version);
  if (check?.status === "ready") return { selectable: true, canRequest: true, state: "verified" };
  if (check?.error_code && NO_AUTH.test(check.error_code)) return blocked;
  if (check?.error_code && TEMPORARY.test(check.error_code)) {
    const cooling = check.checked_at + (check.retry_after_seconds ?? 0) > now;
    return { selectable: explicitRetry, canRequest: explicitRetry && !cooling,
      state: cooling ? "cooldown" : "retest" };
  }
  if (check) return { selectable: explicitRetry, canRequest: explicitRetry, state: "retest" };
  // Compatibility only for legacy summaries without the new host contract.
  // A modern stale/untested configuration must not inherit global "ready".
  if (model.capability_checks === undefined && model.health_status === "ready") {
    return { selectable: true, canRequest: true, state: "verified" };
  }
  return { selectable: explicitRetry, canRequest: explicitRetry, state: "retest" };
}
