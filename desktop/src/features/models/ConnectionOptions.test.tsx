import { describe, expect, it } from "vitest";

import { compatibleDefaultProfile, connectionUrl, isSameOriginModelsEndpoint, normalizeConnectionEndpoint } from "./ConnectionOptions";

describe("compatible connection defaults", () => {
  it("leaves the phase output budget unset for both supported protocols", () => {
    expect(compatibleDefaultProfile("chat_completions").output_limit).toBeNull();
    expect(compatibleDefaultProfile("ollama").output_limit).toBeNull();
  });
});

describe("connection endpoint settings", () => {
  it("adds v1 only for a host-only compatible base URL and preserves existing paths", () => {
    expect(normalizeConnectionEndpoint("https://models.example.test", "chat_completions")).toEqual({
      endpoint: "https://models.example.test/v1",
      endpoint_mode: "base",
      request_url: "https://models.example.test/v1/chat/completions",
    });
    expect(connectionUrl("https://models.example.test/team/v2/", "chat_completions")).toBe("https://models.example.test/team/v2/chat/completions");
  });

  it("normalizes a standard chat endpoint suffix in base mode", () => {
    expect(normalizeConnectionEndpoint("https://models.example.test/team/v2/chat/completions/", "chat_completions")).toEqual({
      endpoint: "https://models.example.test/team/v2",
      endpoint_mode: "base",
      request_url: "https://models.example.test/team/v2/chat/completions",
    });
  });

  it("preserves a full request URL without appending another path", () => {
    expect(normalizeConnectionEndpoint("https://models.example.test/provider/v2/generate/", "chat_completions", "full_request")).toEqual({
      endpoint: "https://models.example.test/provider/v2/generate",
      endpoint_mode: "full_request",
      request_url: "https://models.example.test/provider/v2/generate",
    });
    expect(connectionUrl("https://models.example.test/provider/v2/generate", "chat_completions", "full_request")).toBe("https://models.example.test/provider/v2/generate");
  });

  it("allows only absolute or slash-relative model-list overrides on the request origin", () => {
    const requestUrl = "https://models.example.test/v1/chat/completions";
    expect(isSameOriginModelsEndpoint("/v2/models", requestUrl)).toBe(true);
    expect(isSameOriginModelsEndpoint("https://models.example.test/custom/models", requestUrl)).toBe(true);
    expect(isSameOriginModelsEndpoint("https://other.example.test/models", requestUrl)).toBe(false);
    expect(isSameOriginModelsEndpoint("//other.example.test/models", requestUrl)).toBe(false);
    expect(isSameOriginModelsEndpoint("models", requestUrl)).toBe(false);
  });
});
