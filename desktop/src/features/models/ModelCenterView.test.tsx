import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  cancelModelTest,
  deleteConfiguredModel,
  deleteProviderConnection,
  discoverConnectionModels,
  getModelDirectory,
  listConfiguredModels,
  listModelProviders,
  listProviderConnections,
  rotateProviderConnectionSecret,
  saveConfiguredModel,
  saveProviderConnection,
  setProviderConnectionSecret,
  subscribeModelTestEvents,
  testConfiguredModel,
  testProviderConnection,
} from "../../backend";
import type { ConfiguredModelSummary, ModelTestEvent, ProviderConnectionSummary, ProviderDefinition } from "../../types";
import { ModelCenterView } from "./ModelCenterView";

vi.mock("../../backend", () => ({
  cancelModelTest: vi.fn(),
  deleteConfiguredModel: vi.fn(),
  deleteProviderConnection: vi.fn(),
  discoverConnectionModels: vi.fn(),
  getModelDirectory: vi.fn(),
  listConfiguredModels: vi.fn(),
  listModelProviders: vi.fn(),
  listProviderConnections: vi.fn(),
  openExternalUrl: vi.fn(),
  rotateProviderConnectionSecret: vi.fn(),
  saveConfiguredModel: vi.fn(),
  saveProviderConnection: vi.fn(),
  setProviderConnectionEnabled: vi.fn(),
  setProviderConnectionSecret: vi.fn(),
  subscribeModelTestEvents: vi.fn(),
  testConfiguredModel: vi.fn(),
  testProviderConnection: vi.fn(),
}));

const providers: ProviderDefinition[] = [
  {
    provider_id: "openai", display_name: "OpenAI", category: "cloud", region: "global",
    base_url: "https://api.openai.com/v1", help_url: "https://platform.openai.com/",
    requires_api_key: true, supports_discovery: true, free_hint: false, allowed_hosts: ["api.openai.com"],
    recommended_models: [{ model_id: "gpt-test", alias: "GPT preset", capabilities: ["text_chat", "structured_json"], billing_class: "paid" }],
    models_path: "/models", default_test_model_id: "gpt-test",
  },
  {
    provider_id: "custom", display_name: "Custom endpoint", category: "custom", region: "custom",
    base_url: "https://models.example.test/v1", help_url: "", requires_api_key: false,
    supports_discovery: true, free_hint: false, allowed_hosts: [], recommended_models: [], models_path: "/models", default_test_model_id: null,
  },
];

const connection: ProviderConnectionSummary = {
  connection_id: "openai-main", provider_id: "openai", display_name: "Work account",
  region: "global", endpoint: "https://api.openai.com/v1", credential_version: 2,
  has_secret: true, enabled: true, status: "ready", configuration_version: 3,
};
const secondConnection: ProviderConnectionSummary = {
  ...connection, connection_id: "openai-second", display_name: "Second account", endpoint: "https://other.example.test/v1",
};
const model: ConfiguredModelSummary = {
  configured_model_id: "openai-main.gpt-test", connection_id: connection.connection_id,
  model_id: "gpt-test", alias: "Primary GPT", free_tier: false, billing_class: "paid",
  free_source_url: null, free_verified_at: null, enabled: true,
  capabilities: ["text_chat", "structured_json"], health_status: "ready",
  last_discovered_at: null, context_window_hint: 32000, temperature: 0.25,
  timeout_seconds: 180, configuration_version: 4,
};
const visionModel = { ...model, capabilities: [...model.capabilities, "vision"] };
type CallMetadata<TArgs extends unknown[]> = { mock: { calls: TArgs[]; invocationCallOrder: number[] } };
function callMetadata<TArgs extends unknown[]>(value: unknown) { return (value as CallMetadata<TArgs>).mock; }

describe("ModelCenterView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getModelDirectory).mockResolvedValue({ source: "https://models.dev/api.json", retrieved_at: "2026-10-22", license: "MIT", models: [] });
    vi.mocked(listModelProviders).mockResolvedValue(providers);
    vi.mocked(listProviderConnections).mockResolvedValue([connection]);
    vi.mocked(listConfiguredModels).mockResolvedValue([model]);
    vi.mocked(rotateProviderConnectionSecret).mockResolvedValue(connection);
    vi.mocked(deleteProviderConnection).mockResolvedValue(undefined);
    vi.mocked(deleteConfiguredModel).mockResolvedValue(undefined);
    vi.mocked(discoverConnectionModels).mockResolvedValue([]);
    vi.mocked(saveConfiguredModel).mockResolvedValue(model);
    vi.mocked(saveProviderConnection).mockResolvedValue(connection);
    vi.mocked(setProviderConnectionSecret).mockResolvedValue(connection);
    vi.mocked(testConfiguredModel).mockResolvedValue({ ok: true, message: "Connection succeeded." });
    vi.mocked(subscribeModelTestEvents).mockResolvedValue(vi.fn());
    vi.mocked(cancelModelTest).mockResolvedValue(undefined);
  });

  it("presents a compact connection list and one active detail pane", async () => {
    render(<ModelCenterView language="en" />);

    expect(await screen.findByRole("complementary", { name: "Connections" })).toBeVisible();
    expect(await screen.findByRole("region", { name: "Connection details: Work account" })).toBeVisible();
    expect(screen.getAllByRole("region", { name: /Connection details:/ })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: /Work account/ })[0]).toHaveAttribute("aria-pressed", "true");
  });

  it("keeps the compatible connection form focused on name, address, and optional key", async () => {
    render(<ModelCenterView language="en" />);
    await screen.findByRole("region", { name: "Connection details: Work account" });
    fireEvent.click(screen.getByRole("button", { name: "Add connection" }));

    expect(await screen.findByRole("region", { name: "New connection: Compatible API" })).toBeVisible();
    expect(screen.getByLabelText("Connection name")).toBeVisible();
    expect(screen.getByLabelText("Endpoint")).toBeVisible();
    expect(screen.getByLabelText("API key")).toBeVisible();
    fireEvent.change(screen.getByLabelText("Endpoint"), { target: { value: "https://models.example.com/v1" } });
    expect(screen.getByLabelText("Connection name")).toHaveAttribute("placeholder", "models.example.com");
    expect(screen.getByText("Advanced settings").closest("details")).not.toHaveAttribute("open");
    expect(screen.getByRole("button", { name: "Add a preset connection" })).toBeVisible();
  });

  it("saves the compatible connection and credential before one model-list request", async () => {
    const saved = { ...connection, connection_id: "compatible-new", provider_id: "custom", endpoint: "https://models.example.test/v1", has_secret: true };
    vi.mocked(saveProviderConnection).mockResolvedValue(saved);
    vi.mocked(setProviderConnectionSecret).mockResolvedValue(saved);
    vi.mocked(listProviderConnections).mockResolvedValue([connection, saved]);
    vi.mocked(discoverConnectionModels).mockResolvedValue([{ model_id: "vendor/Exact-ID", alias: "Exact ID", billing_class: "unknown", capabilities: ["text_chat"] }]);
    render(<ModelCenterView language="en" />);
    await screen.findByRole("region", { name: "Connection details: Work account" });
    fireEvent.click(screen.getByRole("button", { name: "Add connection" }));
    await screen.findByRole("region", { name: "New connection: Compatible API" });
    fireEvent.change(screen.getByLabelText("Endpoint"), { target: { value: "https://models.example.test" } });
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "one-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Save and get models" }));

    await screen.findByRole("dialog", { name: "Manage models" });
    expect(saveProviderConnection).toHaveBeenCalledWith(expect.objectContaining({
      provider_id: "custom", endpoint: "https://models.example.test/v1", protocol: "chat_completions",
      capability_profile: expect.objectContaining({ source: "compatible_default", stream: "sse", json_output: "prompt_json", output_parameter: "max_tokens", output_semantics: "visible_output_tokens_only", allow_unbounded_output: false, include_usage: false, usage: "unknown", upstream: "unknown" }),
      transport: { endpoint_mode: "base", models_endpoint: null, network_mode: "system", proxy_url: null },
    }));
    expect(callMetadata<[Parameters<typeof saveProviderConnection>[0]]>(saveProviderConnection).invocationCallOrder[0]).toBeLessThan(callMetadata<[string, string]>(setProviderConnectionSecret).invocationCallOrder[0]);
    expect(callMetadata<[string, string]>(setProviderConnectionSecret).invocationCallOrder[0]).toBeLessThan(callMetadata<[string]>(discoverConnectionModels).invocationCallOrder[0]);
    expect(discoverConnectionModels).toHaveBeenCalledTimes(1);
    expect(testConfiguredModel).not.toHaveBeenCalled();
    expect(testProviderConnection).not.toHaveBeenCalled();
  });

  it("offers a save-only path without discovery or a model test", async () => {
    const saved = { ...connection, connection_id: "compatible-new", provider_id: "custom", endpoint: "https://models.example.test/v1" };
    vi.mocked(saveProviderConnection).mockResolvedValue(saved);
    render(<ModelCenterView language="en" />);
    await screen.findByRole("region", { name: "Connection details: Work account" });
    fireEvent.click(screen.getByRole("button", { name: "Add connection" }));
    await screen.findByRole("region", { name: "New connection: Compatible API" });
    fireEvent.change(screen.getByLabelText("Endpoint"), { target: { value: "https://models.example.test" } });
    fireEvent.click(screen.getByRole("button", { name: "Save only" }));

    await waitFor(() => expect(saveProviderConnection).toHaveBeenCalledTimes(1));
    expect(discoverConnectionModels).not.toHaveBeenCalled();
    expect(testConfiguredModel).not.toHaveBeenCalled();
    expect(testProviderConnection).not.toHaveBeenCalled();
  });

  it("keeps legacy custom capability settings unknown and omits them on a rename-only save", async () => {
    const legacyCustom = {
      ...connection,
      connection_id: "custom-legacy",
      provider_id: "custom",
      display_name: "Legacy custom",
      endpoint: "https://legacy.example.test/v1",
      protocol: "chat_completions" as const,
      capability_profile: null,
    };
    vi.mocked(listProviderConnections).mockResolvedValue([legacyCustom]);
    vi.mocked(listConfiguredModels).mockResolvedValue([]);
    vi.mocked(saveProviderConnection).mockResolvedValue(legacyCustom);
    render(<ModelCenterView language="en" />);
    await screen.findByRole("region", { name: "Connection details: Legacy custom" });
    fireEvent.click(screen.getByRole("button", { name: "Edit connection" }));
    await screen.findByRole("region", { name: "New connection: Compatible API" });
    fireEvent.click(screen.getByText("Advanced settings"));
    fireEvent.click(screen.getByText("Capability and budget policy"));
    expect(screen.getByText("Contract source: Legacy setting")).toBeVisible();
    fireEvent.change(screen.getByLabelText("Connection name"), { target: { value: "Renamed legacy" } });
    fireEvent.click(screen.getByRole("button", { name: "Save only" }));

    await waitFor(() => expect(saveProviderConnection).toHaveBeenCalledTimes(1));
    expect("capability_profile" in vi.mocked(saveProviderConnection).mock.calls[0][0]).toBe(false);
  });

  it("retains an existing explicit output limit when saving connection details", async () => {
    const cappedConnection: ProviderConnectionSummary = {
      ...connection,
      connection_id: "custom-capped",
      provider_id: "custom",
      display_name: "Capped endpoint",
      endpoint: "https://legacy.example.test/v1",
      capability_profile: {
        source: "user", json_output: "prompt_json", stream: "sse", include_usage: false,
        usage: "unknown", output_parameter: "max_tokens", output_semantics: "visible_output_tokens_only",
        output_limit: 8192, allow_unbounded_output: false, upstream: "unknown",
      },
    };
    vi.mocked(listProviderConnections).mockResolvedValue([cappedConnection]);
    vi.mocked(listConfiguredModels).mockResolvedValue([]);
    vi.mocked(saveProviderConnection).mockResolvedValue(cappedConnection);
    render(<ModelCenterView language="en" />);
    await screen.findByRole("region", { name: "Connection details: Capped endpoint" });
    fireEvent.click(screen.getByRole("button", { name: "Edit connection" }));
    fireEvent.click(screen.getByText("Advanced settings"));
    fireEvent.click(screen.getByText("Capability and budget policy"));
    expect(screen.getByLabelText("Requested output limit")).toHaveValue(8192);

    fireEvent.change(screen.getByLabelText("Connection name"), { target: { value: "Renamed capped endpoint" } });
    fireEvent.click(screen.getByRole("button", { name: "Save only" }));
    await waitFor(() => expect(saveProviderConnection).toHaveBeenCalledWith(expect.objectContaining({
      display_name: "Renamed capped endpoint",
      capability_profile: expect.objectContaining({ output_limit: 8192 }),
    })));
  });

  it("updates compatible defaults when switching a custom connection to Ollama", async () => {
    render(<ModelCenterView language="en" />);
    await screen.findByRole("region", { name: "Connection details: Work account" });
    fireEvent.click(screen.getByRole("button", { name: "Add connection" }));
    await screen.findByRole("region", { name: "New connection: Compatible API" });
    fireEvent.change(screen.getByLabelText("Endpoint"), { target: { value: "http://localhost:11434" } });
    fireEvent.click(screen.getByText("Advanced settings"));
    fireEvent.click(screen.getByText("Request address"));
    fireEvent.click(screen.getByText("Capability and budget policy"));
    fireEvent.change(screen.getByLabelText("Interface type"), { target: { value: "ollama" } });

    expect(screen.getByLabelText("Stream framing")).toHaveValue("ndjson");
    expect(screen.getByLabelText("Output limit parameter")).toHaveValue("num_predict");
    fireEvent.click(screen.getByRole("button", { name: "Save only" }));
    await waitFor(() => expect(saveProviderConnection).toHaveBeenCalledWith(expect.objectContaining({
      protocol: "ollama",
      capability_profile: expect.objectContaining({ source: "compatible_default", stream: "ndjson", output_parameter: "num_predict" }),
    })));
  });

  it("rejects a model-list override on another origin before saving credentials", async () => {
    render(<ModelCenterView language="en" />);
    await screen.findByRole("region", { name: "Connection details: Work account" });
    fireEvent.click(screen.getByRole("button", { name: "Add connection" }));
    await screen.findByRole("region", { name: "New connection: Compatible API" });
    fireEvent.change(screen.getByLabelText("Endpoint"), { target: { value: "https://models.example.test/v1" } });
    fireEvent.click(screen.getByText("Advanced settings"));
    fireEvent.click(screen.getByText("Request address"));
    fireEvent.change(screen.getByPlaceholderText("/models"), { target: { value: "https://other.example.test/models" } });
    fireEvent.click(screen.getByRole("button", { name: "Save only" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("same origin");
    expect(saveProviderConnection).not.toHaveBeenCalled();
    expect(setProviderConnectionSecret).not.toHaveBeenCalled();
  });

  it("saves a full request URL without normalizing away its vendor path", async () => {
    render(<ModelCenterView language="en" />);
    await screen.findByRole("region", { name: "Connection details: Work account" });
    fireEvent.click(screen.getByRole("button", { name: "Add connection" }));
    await screen.findByRole("region", { name: "New connection: Compatible API" });
    fireEvent.click(screen.getByText("Advanced settings"));
    fireEvent.click(screen.getByText("Request address"));
    fireEvent.change(screen.getByLabelText("Request address format"), { target: { value: "full_request" } });
    fireEvent.change(screen.getByLabelText("Endpoint"), { target: { value: "https://models.example.test/vendor/v2/generate" } });
    fireEvent.click(screen.getByRole("button", { name: "Save only" }));

    await waitFor(() => expect(saveProviderConnection).toHaveBeenCalledWith(expect.objectContaining({
      endpoint: "https://models.example.test/vendor/v2/generate",
      transport: expect.objectContaining({ endpoint_mode: "full_request" }),
    })));
  });

  it("retains the saved connection draft if secure credential storage fails", async () => {
    const saved = { ...connection, connection_id: "custom-draft", provider_id: "custom" };
    vi.mocked(saveProviderConnection).mockResolvedValue(saved);
    vi.mocked(setProviderConnectionSecret).mockRejectedValueOnce(new Error("vault unavailable"));
    render(<ModelCenterView language="en" />);
    await screen.findByRole("region", { name: "Connection details: Work account" });
    fireEvent.click(screen.getByRole("button", { name: "Add connection" }));
    await screen.findByRole("region", { name: "New connection: Compatible API" });
    fireEvent.change(screen.getByLabelText("Endpoint"), { target: { value: "https://models.example.test" } });
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "test-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Save only" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("vault unavailable");
    expect(screen.getByLabelText("API key")).toHaveValue("test-secret");
    fireEvent.click(screen.getByRole("button", { name: "Save only" }));
    await waitFor(() => expect(saveProviderConnection).toHaveBeenLastCalledWith(expect.objectContaining({ connection_id: "custom-draft" })));
  });

  it("filters discovery results and adds exact raw IDs with clear source metadata", async () => {
    vi.mocked(discoverConnectionModels).mockResolvedValue([
      { model_id: "vendor/Exact-ID", alias: "Exact ID", billing_class: "paid", capabilities: ["text_chat"] },
      { model_id: "another/hidden", alias: "Hidden model", billing_class: "unknown", capabilities: ["text_chat"] },
    ]);
    render(<ModelCenterView language="en" />);
    await screen.findByRole("region", { name: "Connection details: Work account" });
    fireEvent.click(screen.getByRole("button", { name: "Get model list" }));

    const dialog = await screen.findByRole("dialog", { name: "Manage models" });
    fireEvent.change(within(dialog).getByLabelText("Search models"), { target: { value: "Exact-ID" } });
    expect(within(dialog).getByText("vendor/Exact-ID")).toBeVisible();
    expect(within(dialog).getByText(/Returned by API/)).toBeVisible();
    expect(within(dialog).queryByText("Hidden model")).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "Select model: Exact ID" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Add selected models (1)" }));

    await waitFor(() => expect(saveConfiguredModel).toHaveBeenCalledWith(expect.objectContaining({ model_id: "vendor/Exact-ID", alias: "Exact ID" })));
    expect(testConfiguredModel).not.toHaveBeenCalled();
  });

  it("keeps manual models available when discovery fails", async () => {
    vi.mocked(discoverConnectionModels).mockRejectedValue(new Error("404 metadata unavailable"));
    render(<ModelCenterView language="en" />);
    await screen.findByRole("region", { name: "Connection details: Work account" });
    fireEvent.change(screen.getByLabelText("Manual model ID"), { target: { value: "My/PRIVATE" } });
    fireEvent.click(screen.getByRole("button", { name: "Add candidate" }));
    const dialog = await screen.findByRole("dialog", { name: "Manage models" });
    expect(within(dialog).getAllByText("My/PRIVATE")[0]).toBeVisible();
    expect(within(dialog).getByText("Manual model ID")).toBeVisible();
    fireEvent.keyDown(window, { key: "Escape" });
    fireEvent.click(screen.getByRole("button", { name: "Get model list" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("does not offer a model list");
    fireEvent.change(screen.getByLabelText("Manual model ID"), { target: { value: "My/PRIVATE" } });
    fireEvent.click(screen.getByRole("button", { name: "Add candidate" }));
    const retainedDialog = await screen.findByRole("dialog", { name: "Manage models" });
    expect(within(retainedDialog).getAllByText("My/PRIVATE")[0]).toBeVisible();
    expect(within(retainedDialog).getByRole("checkbox", { name: "Select model: My/PRIVATE" })).toBeChecked();
    fireEvent.click(within(retainedDialog).getByRole("button", { name: "Add selected models (1)" }));

    await waitFor(() => expect(saveConfiguredModel).toHaveBeenCalledWith(expect.objectContaining({ model_id: "My/PRIVATE" })));
  });

  it("does not overwrite an existing model's user configuration when its ID is re-added", async () => {
    const existing = { ...visionModel, alias: "My GPT alias", enabled: false, timeout_seconds: 321, context_window_hint: 12000, temperature: 0.35 };
    vi.mocked(listConfiguredModels).mockResolvedValue([existing]);
    vi.mocked(discoverConnectionModels).mockResolvedValue([{ model_id: existing.model_id, alias: "Catalog alias", billing_class: "paid", capabilities: ["text_chat"] }]);
    render(<ModelCenterView language="en" />);
    await screen.findByRole("region", { name: "Connection details: Work account" });
    fireEvent.click(screen.getByRole("button", { name: "Get model list" }));
    const dialog = await screen.findByRole("dialog", { name: "Manage models" });

    expect(within(dialog).getByRole("button", { name: "Already added" })).toBeDisabled();
    expect(within(dialog).getByRole("checkbox", { name: "Select model: Catalog alias" })).toBeDisabled();
    expect(screen.getByText("My GPT alias")).toBeVisible();
    expect(screen.getByLabelText("Request timeout (seconds): My GPT alias")).toHaveValue(321);
    expect(saveConfiguredModel).not.toHaveBeenCalled();
  });

  it("allows model-list lookup for a provider without a catalog when a same-origin override is saved", async () => {
    const providerWithoutCatalog = { ...providers[0], supports_discovery: false };
    const connectionWithOverride = {
      ...connection,
      transport: { endpoint_mode: "base" as const, models_endpoint: "/custom/models", network_mode: "system" as const, proxy_url: null },
    };
    vi.mocked(listModelProviders).mockResolvedValue([providerWithoutCatalog, providers[1]]);
    vi.mocked(listProviderConnections).mockResolvedValue([connectionWithOverride]);
    render(<ModelCenterView language="en" />);
    await screen.findByRole("region", { name: "Connection details: Work account" });
    const button = screen.getByRole("button", { name: "Get model list" });
    expect(button).toBeEnabled();
    fireEvent.click(button);
    await waitFor(() => expect(discoverConnectionModels).toHaveBeenCalledWith(connection.connection_id));
  });

  it("keeps model-list lookup disabled for a provider without a catalog and no override", async () => {
    const providerWithoutCatalog = { ...providers[0], supports_discovery: false };
    vi.mocked(listModelProviders).mockResolvedValue([providerWithoutCatalog, providers[1]]);
    vi.mocked(listProviderConnections).mockResolvedValue([connection]);
    render(<ModelCenterView language="en" />);
    await screen.findByRole("region", { name: "Connection details: Work account" });
    expect(screen.getByRole("button", { name: "Get model list" })).toBeDisabled();
    expect(discoverConnectionModels).not.toHaveBeenCalled();
  });

  it("saves a model timeout without changing its alias, temperature, capacity, or capabilities", async () => {
    render(<ModelCenterView language="en" />);
    await screen.findByRole("region", { name: "Connection details: Work account" });
    fireEvent.change(screen.getByLabelText("Request timeout (seconds): Primary GPT"), { target: { value: "240" } });
    fireEvent.click(screen.getByRole("button", { name: "Save timeout" }));

    await waitFor(() => expect(saveConfiguredModel).toHaveBeenCalledWith({
      configured_model_id: model.configured_model_id, connection_id: model.connection_id,
      model_id: model.model_id, alias: model.alias, enabled: model.enabled,
      capabilities: model.capabilities, timeout_seconds: 240,
      context_window_hint: model.context_window_hint, temperature: model.temperature,
    }));
  });

  it("offers connection dialogs with managed focus and does not expose the stored secret", async () => {
    render(<ModelCenterView language="en" />);
    await screen.findByRole("region", { name: "Connection details: Work account" });
    fireEvent.click(screen.getByRole("button", { name: "Replace key" }));
    const dialog = screen.getByRole("dialog", { name: "Replace the saved API key" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.queryByText(/previous-secret/i)).not.toBeInTheDocument();
    expect(within(dialog).getByLabelText("API key")).toHaveFocus();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Replace key" })).toHaveFocus());
  });

  it("tests a configured model once and shows only the progress event state, not content", async () => {
    let receiveEvent!: (event: ModelTestEvent) => void;
    let resolveTest!: (result: { ok: boolean; message: string }) => void;
    vi.mocked(subscribeModelTestEvents).mockImplementation(async (handler) => { receiveEvent = handler; return vi.fn() as unknown as () => void; });
    vi.mocked(testConfiguredModel).mockImplementation(() => new Promise((resolve) => { resolveTest = resolve; }));
    render(<ModelCenterView language="en" />);
    await screen.findByRole("region", { name: "Connection details: Work account" });
    fireEvent.click(screen.getByRole("button", { name: "Test: Primary GPT" }));

    await waitFor(() => expect(testConfiguredModel).toHaveBeenCalledTimes(1));
    expect(callMetadata<[(event: ModelTestEvent) => void]>(subscribeModelTestEvents).invocationCallOrder[0]).toBeLessThan(callMetadata<[string, boolean?, string?]>(testConfiguredModel).invocationCallOrder[0]);
    expect(callMetadata<[string, boolean?, string?]>(testConfiguredModel).calls[0][0]).toBe(model.configured_model_id);
    expect(callMetadata<[string, boolean?, string?]>(testConfiguredModel).calls[0][1]).toBe(false);
    const operationId = callMetadata<[string, boolean?, string?]>(testConfiguredModel).calls[0][2];
    expect(operationId).toEqual(expect.any(String));
    expect(screen.getByRole("button", { name: "Stop waiting" })).toBeVisible();
    receiveEvent({ protocol: "model-events-v1", context: { request_id: operationId!, attempt_id: operationId!, run_id: model.configured_model_id, generation: model.configuration_version, stage_id: "test" }, seq: 1, kind: "activity", data: { state: "reasoning" } });
    expect(await screen.findByText("Thinking")).toBeVisible();
    receiveEvent({ protocol: "model-events-v1", context: { request_id: operationId!, attempt_id: operationId!, run_id: model.configured_model_id, generation: model.configuration_version, stage_id: "test" }, seq: 2, kind: "content", data: { content: "do not reveal this answer" } });
    expect(await screen.findByText("Receiving response")).toBeVisible();
    expect(screen.queryByText("do not reveal this answer")).not.toBeInTheDocument();

    resolveTest({ ok: true, message: "Connection succeeded." });
    expect(await screen.findByText("Connection check succeeded; full research capacity was not benchmarked.")).toBeVisible();
    expect(testProviderConnection).not.toHaveBeenCalled();
  });

  it("does not mark a failed result event successful and releases its subscription when testing settles", async () => {
    let receiveEvent!: (event: ModelTestEvent) => void;
    let resolveTest!: (result: { ok: boolean; message: string }) => void;
    const unlisten = vi.fn();
    vi.mocked(subscribeModelTestEvents).mockImplementation(async (handler) => { receiveEvent = handler; return unlisten; });
    vi.mocked(testConfiguredModel).mockImplementation(() => new Promise((resolve) => { resolveTest = resolve; }));
    render(<ModelCenterView language="en" />);
    await screen.findByRole("region", { name: "Connection details: Work account" });
    fireEvent.click(screen.getByRole("button", { name: "Test: Primary GPT" }));
    await waitFor(() => expect(testConfiguredModel).toHaveBeenCalledTimes(1));
    const operationId = callMetadata<[string, boolean?, string?]>(testConfiguredModel).calls[0][2]!;
    receiveEvent({ protocol: "model-events-v1", context: { request_id: operationId, attempt_id: operationId, run_id: model.configured_model_id, generation: model.configuration_version, stage_id: "test" }, seq: 1, kind: "result", data: { ok: false } });

    expect((await screen.findAllByText("Test failed")).length).toBeGreaterThan(0);
    expect(screen.queryByText("Test completed")).not.toBeInTheDocument();
    resolveTest({ ok: false, message: "Model test failed." });
    await waitFor(() => expect(unlisten).toHaveBeenCalledTimes(1));
  });

  it("shows heartbeat as connection activity after reasoning without retaining the thinking label", async () => {
    let receiveEvent!: (event: ModelTestEvent) => void;
    let resolveTest!: (result: { ok: boolean; message: string }) => void;
    vi.mocked(subscribeModelTestEvents).mockImplementation(async (handler) => { receiveEvent = handler; return vi.fn() as unknown as () => void; });
    vi.mocked(testConfiguredModel).mockImplementation(() => new Promise((resolve) => { resolveTest = resolve; }));
    render(<ModelCenterView language="en" />);
    await screen.findByRole("region", { name: "Connection details: Work account" });
    fireEvent.click(screen.getByRole("button", { name: "Test: Primary GPT" }));
    await waitFor(() => expect(testConfiguredModel).toHaveBeenCalledTimes(1));
    const operationId = callMetadata<[string, boolean?, string?]>(testConfiguredModel).calls[0][2]!;
    const event = (seq: number, state: string): ModelTestEvent => ({ protocol: "model-events-v1", context: { request_id: operationId, attempt_id: operationId, run_id: model.configured_model_id, generation: model.configuration_version, stage_id: "test" }, seq, kind: "activity", data: { state } });
    receiveEvent(event(1, "reasoning"));
    expect(await screen.findByText("Thinking")).toBeVisible();
    receiveEvent(event(2, "heartbeat"));

    expect(await screen.findByText("Connection remains active")).toBeVisible();
    expect(screen.queryByText("Thinking")).not.toBeInTheDocument();
    resolveTest({ ok: false, message: "test finished" });
  });

  it.each([
    ["en", "Text connection completed, but the selected capability check did not pass. Review this model's capability check results.", "No retry was sent. Disable streaming or choose a stream format supported by this service before testing again."],
    ["zh-CN", "文字连接已完成，但所选能力检查未通过，请查看该模型的能力检查结果。", "没有发送重试请求。请关闭流式协议，或选择此服务支持的流式格式后再测试。"],
    ["zh-Hant", "文字連線已完成，但所選能力檢查未通過，請查看此模型的能力檢查結果。", "沒有傳送重試請求。請關閉串流協定，或選擇此服務支援的串流格式後再測試。"],
  ] as const)("localizes a failed capability result without exposing raw warnings in %s", async (language, generic, safeError) => {
    vi.mocked(testConfiguredModel).mockResolvedValue({
      ok: false,
      message: "Raw English upstream warnings must not be shown.",
      capabilities: [{ capability: "text_chat", status: "error", error_code: "MODEL_STREAM_UNSUPPORTED" }],
    });
    render(<ModelCenterView language={language} />);
    await screen.findByRole("region", { name: /Connection details:/ });
    const testLabel = language === "en" ? "Test: Primary GPT" : language === "zh-Hant" ? "測試: Primary GPT" : "测试: Primary GPT";
    fireEvent.click(screen.getByRole("button", { name: testLabel }));

    const progress = await screen.findByRole("status");
    await waitFor(() => {
      expect(progress).toHaveTextContent(generic);
      expect(progress).toHaveTextContent(safeError);
    });
    expect(progress).not.toHaveTextContent("Raw English upstream warnings must not be shown.");
  });

  it.each([
    ["en", "Text connection completed, but the selected capability check did not pass. Review this model's capability check results."],
    ["zh-CN", "文字连接已完成，但所选能力检查未通过，请查看该模型的能力检查结果。"],
    ["zh-Hant", "文字連線已完成，但所選能力檢查未通過，請查看此模型的能力檢查結果。"],
  ] as const)("shows only the localized generic result when failed capabilities have no safe error code in %s", async (language, generic) => {
    vi.mocked(testConfiguredModel).mockResolvedValue({
      ok: false,
      message: "Raw English warnings must not be shown.",
      capabilities: [{ capability: "text_chat", status: "error", error_code: null }],
    });
    render(<ModelCenterView language={language} />);
    await screen.findByRole("region", { name: /Connection details:/ });
    const testLabel = language === "en" ? "Test: Primary GPT" : language === "zh-Hant" ? "測試: Primary GPT" : "测试: Primary GPT";
    fireEvent.click(screen.getByRole("button", { name: testLabel }));

    const progress = await screen.findByRole("status");
    await waitFor(() => expect(progress).toHaveTextContent(generic));
    expect(progress).not.toHaveTextContent("Raw English warnings must not be shown.");
    expect(progress).not.toHaveTextContent("No retry was sent.");
  });

  it.each([
    ["en", "No follow-up request was sent. Change the output limit parameter to one supported by this service before testing again."],
    ["zh-CN", "没有发送后续请求。请将输出上限参数改为此服务支持的参数后再测试。"],
    ["zh-Hant", "沒有傳送後續請求。請將輸出上限參數改為此服務支援的參數後再測試。"],
  ] as const)("localizes an error event code in %s", async (language, safeError) => {
    let receiveEvent!: (event: ModelTestEvent) => void;
    let resolveTest!: (result: { ok: boolean; message: string }) => void;
    vi.mocked(subscribeModelTestEvents).mockImplementation(async (handler) => { receiveEvent = handler; return vi.fn() as unknown as () => void; });
    vi.mocked(testConfiguredModel).mockImplementation(() => new Promise((resolve) => { resolveTest = resolve; }));
    render(<ModelCenterView language={language} />);
    await screen.findByRole("region", { name: /Connection details:/ });
    const testLabel = language === "en" ? "Test: Primary GPT" : language === "zh-Hant" ? "測試: Primary GPT" : "测试: Primary GPT";
    fireEvent.click(screen.getByRole("button", { name: testLabel }));
    await waitFor(() => expect(testConfiguredModel).toHaveBeenCalledTimes(1));
    const operationId = callMetadata<[string, boolean?, string?]>(testConfiguredModel).calls[0][2]!;
    receiveEvent({ protocol: "model-events-v1", context: { request_id: operationId, attempt_id: operationId, run_id: model.configured_model_id, generation: model.configuration_version, stage_id: "test" }, seq: 1, kind: "error", data: { code: "MODEL_OUTPUT_PARAMETER_UNSUPPORTED" } });

    expect(await screen.findByText(new RegExp(safeError.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")))).toBeVisible();
    resolveTest({ ok: false, message: "raw event warning" });
  });

  it("stops a pending model-test wait once without resending the model request", async () => {
    let resolveTest!: (result: { ok: boolean; message: string }) => void;
    vi.mocked(testConfiguredModel).mockImplementation(() => new Promise((resolve) => { resolveTest = resolve; }));
    render(<ModelCenterView language="en" />);
    await screen.findByRole("region", { name: "Connection details: Work account" });
    fireEvent.click(screen.getByRole("button", { name: "Test: Primary GPT" }));
    await waitFor(() => expect(testConfiguredModel).toHaveBeenCalledTimes(1));
    const operationId = callMetadata<[string, boolean?, string?]>(testConfiguredModel).calls[0][2]!;
    fireEvent.click(await screen.findByRole("button", { name: "Stop waiting" }));

    await waitFor(() => expect(cancelModelTest).toHaveBeenCalledWith(operationId));
    expect(testConfiguredModel).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("Stopped waiting; no retry was sent.")).toBeVisible();
    resolveTest({ ok: true, message: "late success" });
    await waitFor(() => expect(screen.queryByText("Connection check succeeded")).not.toBeInTheDocument());
  });

  it("ignores late model-test progress after switching connections", async () => {
    let receiveEvent!: (event: ModelTestEvent) => void;
    let resolveTest!: (result: { ok: boolean; message: string }) => void;
    vi.mocked(listProviderConnections).mockResolvedValue([connection, secondConnection]);
    vi.mocked(subscribeModelTestEvents).mockImplementation(async (handler) => { receiveEvent = handler; return vi.fn() as unknown as () => void; });
    vi.mocked(testConfiguredModel).mockImplementation(() => new Promise((resolve) => { resolveTest = resolve; }));
    render(<ModelCenterView language="en" />);
    await screen.findByRole("region", { name: "Connection details: Work account" });
    fireEvent.click(screen.getByRole("button", { name: "Test: Primary GPT" }));
    await waitFor(() => expect(testConfiguredModel).toHaveBeenCalledTimes(1));
    const operationId = callMetadata<[string, boolean?, string?]>(testConfiguredModel).calls[0][2]!;
    const connectionList = screen.getByRole("complementary", { name: "Connections" });
    fireEvent.click(within(connectionList).getAllByRole("button", { name: /Second account/ })[0]);
    await screen.findByRole("region", { name: "Connection details: Second account" });
    receiveEvent({ protocol: "model-events-v1", context: { request_id: operationId, attempt_id: operationId, run_id: model.configured_model_id, generation: model.configuration_version, stage_id: "test" }, seq: 1, kind: "activity", data: { state: "reasoning" } });
    resolveTest({ ok: true, message: "late success" });

    await waitFor(() => expect(screen.queryByText("Thinking")).not.toBeInTheDocument());
    expect(screen.queryByText("Connection check succeeded")).not.toBeInTheDocument();
  });

  it("runs the visual test only through its separate explicit action", async () => {
    vi.mocked(listConfiguredModels).mockResolvedValue([visionModel]);
    render(<ModelCenterView language="en" />);
    await screen.findByRole("region", { name: "Connection details: Work account" });
    fireEvent.click(screen.getByRole("button", { name: "Test vision (2 requests): Primary GPT" }));

    await waitFor(() => expect(testConfiguredModel).toHaveBeenCalledTimes(1));
    expect(callMetadata<[string, boolean?, string?]>(testConfiguredModel).calls[0][0]).toBe(visionModel.configured_model_id);
    expect(callMetadata<[string, boolean?, string?]>(testConfiguredModel).calls[0][1]).toBe(true);
    expect(testProviderConnection).not.toHaveBeenCalled();
  });

  it.each([
    ["en", "MODEL_DISCOVERY_UNSUPPORTED", "Enter the exact model ID manually", "discover"] as const,
    ["zh-CN", "MODEL_DISCOVERY_UNSUPPORTED", "请手动输入准确的模型 ID", "discover"] as const,
    ["zh-Hant", "MODEL_DISCOVERY_UNSUPPORTED", "請手動輸入正確的模型 ID", "discover"] as const,
    ["en", "MODEL_STREAM_UNSUPPORTED", "Disable streaming or choose a stream format supported", "test"] as const,
    ["zh-CN", "MODEL_STREAM_UNSUPPORTED", "请关闭流式协议", "test"] as const,
    ["zh-Hant", "MODEL_STREAM_UNSUPPORTED", "請關閉串流協定", "test"] as const,
    ["en", "MODEL_OUTPUT_PARAMETER_UNSUPPORTED", "Change the output limit parameter to one supported", "test"] as const,
    ["zh-CN", "MODEL_OUTPUT_PARAMETER_UNSUPPORTED", "请将输出上限参数改为此服务支持的参数", "test"] as const,
    ["zh-Hant", "MODEL_OUTPUT_PARAMETER_UNSUPPORTED", "請將輸出上限參數改為此服務支援的參數", "test"] as const,
    ["en", "MODEL_MODEL_NOT_FOUND", "Check its spelling and the provider's model list", "test"] as const,
    ["zh-CN", "MODEL_MODEL_NOT_FOUND", "请核对拼写和服务商模型列表", "test"] as const,
    ["zh-Hant", "MODEL_MODEL_NOT_FOUND", "請核對拼寫與服務商模型清單", "test"] as const,
  ])("shows an actionable %s message for %s", async (language, code, expected, operation) => {
    const providerError = Object.assign(new Error("raw provider error"), { code });
    if (operation === "discover") vi.mocked(discoverConnectionModels).mockRejectedValueOnce(providerError);
    else vi.mocked(testConfiguredModel).mockRejectedValueOnce(providerError);
    render(<ModelCenterView language={language} />);
    await screen.findByRole("region", { name: /Work account|Legacy/ });
    if (operation === "discover") {
      const label = language === "en" ? "Get model list" : language === "zh-Hant" ? "取得模型清單" : "获取模型列表";
      fireEvent.click(screen.getByRole("button", { name: label }));
      expect(await screen.findByRole("alert")).toHaveTextContent(expected);
    } else {
      const label = language === "en" ? "Test: Primary GPT" : language === "zh-Hant" ? "測試: Primary GPT" : "测试: Primary GPT";
      fireEvent.click(screen.getByRole("button", { name: label }));
      expect(await screen.findByText(new RegExp(expected))).toBeVisible();
      if (code === "MODEL_OUTPUT_PARAMETER_UNSUPPORTED") expect(testConfiguredModel).toHaveBeenCalledTimes(1);
    }
  });

  it.each(["zh-CN", "zh-Hant"] as const)("localizes model management in %s", async (language) => {
    render(<ModelCenterView language={language} />);
    const heading = language === "zh-Hant" ? "連線" : "连接";
    const discover = language === "zh-Hant" ? "取得模型清單" : "获取模型列表";
    expect(await screen.findByRole("complementary", { name: heading })).toBeVisible();
    expect(screen.getByRole("button", { name: discover })).toBeVisible();
  });
});
