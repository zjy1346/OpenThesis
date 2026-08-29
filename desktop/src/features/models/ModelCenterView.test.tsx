import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  deleteConfiguredModel,
  deleteProviderConnection,
  discoverConnectionModels,
  listConfiguredModels,
  listModelProviders,
  listProviderConnections,
  rotateProviderConnectionSecret,
  saveConfiguredModel,
  saveProviderConnection,
  testConfiguredModel,
  testProviderConnection,
} from "../../backend";
import type { ConfiguredModelSummary, ProviderConnectionSummary, ProviderDefinition } from "../../types";
import { ModelCenterView } from "./ModelCenterView";

vi.mock("../../backend", () => ({
  deleteConfiguredModel: vi.fn(),
  deleteProviderConnection: vi.fn(),
  discoverConnectionModels: vi.fn(),
  listConfiguredModels: vi.fn(),
  listModelProviders: vi.fn(),
  listProviderConnections: vi.fn(),
  openExternalUrl: vi.fn(),
  rotateProviderConnectionSecret: vi.fn(),
  saveConfiguredModel: vi.fn(),
  saveProviderConnection: vi.fn(),
  setProviderConnectionEnabled: vi.fn(),
  setProviderConnectionSecret: vi.fn(),
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
    supports_discovery: true, free_hint: false, allowed_hosts: [],
    recommended_models: [], models_path: "/models", default_test_model_id: null,
  },
  {
    provider_id: "glm", display_name: "GLM", category: "cloud", region: "global",
    base_url: "https://open.bigmodel.cn/api/paas/v4", help_url: "", requires_api_key: true,
    supports_discovery: false, free_hint: false, allowed_hosts: ["open.bigmodel.cn"],
    recommended_models: [{ model_id: "glm-5.2", alias: "GLM 5.2", capabilities: ["text_chat", "structured_json"], billing_class: "paid" }],
    models_path: null, default_test_model_id: "glm-5.2",
  },
];

const connection: ProviderConnectionSummary = {
  connection_id: "openai-main", provider_id: "openai", display_name: "Work account",
  region: "global", endpoint: "https://api.openai.com/v1", credential_version: 2,
  has_secret: true, enabled: true, status: "ready", configuration_version: 3,
};

const model: ConfiguredModelSummary = {
  configured_model_id: "openai-main.gpt-test", connection_id: connection.connection_id,
  model_id: "gpt-test", alias: "Primary GPT", free_tier: false, billing_class: "paid",
  free_source_url: null, free_verified_at: null, enabled: true,
  capabilities: ["text_chat", "structured_json"], health_status: "ready",
  last_discovered_at: null, context_window_hint: null, temperature: null,
  timeout_seconds: 180, configuration_version: 4,
};

describe("ModelCenterView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listModelProviders).mockResolvedValue(providers);
    vi.mocked(listProviderConnections).mockResolvedValue([connection]);
    vi.mocked(listConfiguredModels).mockResolvedValue([model]);
    vi.mocked(rotateProviderConnectionSecret).mockResolvedValue(connection);
    vi.mocked(deleteProviderConnection).mockResolvedValue(undefined);
    vi.mocked(deleteConfiguredModel).mockResolvedValue(undefined);
    vi.mocked(discoverConnectionModels).mockResolvedValue([]);
    vi.mocked(saveConfiguredModel).mockResolvedValue(model);
    vi.mocked(testConfiguredModel).mockResolvedValue({ ok: true, message: "Connection succeeded." });
    vi.mocked(testProviderConnection).mockResolvedValue({ ok: true, message: "Connection test succeeded with gpt-test." });
  });


  it("renders local provider artwork and a neutral custom fallback", async () => {
    render(<ModelCenterView language="en" />);

    const openaiLogo = await screen.findByRole("img", { name: "OpenAI logo" });
    expect(openaiLogo.querySelector("img")).toHaveAttribute("src", expect.stringContaining("openai"));

    const customLogo = screen.getByRole("img", { name: "Custom endpoint logo" });
    expect(customLogo.querySelector("img")).toBeNull();
    expect(customLogo.querySelector("svg")).not.toBeNull();
  });
  it("uses the bundled Zhipu asset for the GLM provider", async () => {
    render(<ModelCenterView language="en" />);
    const glmLogo = await screen.findByRole("img", { name: "GLM logo" });
    expect(glmLogo.querySelector("img")).toHaveAttribute("src", expect.stringContaining("glm"));
  });

  it("rotates a secret in an accessible dialog without reading the previous secret", async () => {
    render(<ModelCenterView language="en" />);
    fireEvent.click(await screen.findByRole("button", { name: /OpenAI/ }));
    const replace = screen.getByRole("button", { name: "Replace key" });
    fireEvent.click(replace);

    const dialog = screen.getByRole("dialog", { name: "Replace the saved API key" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.queryByText(/previous-secret/i)).not.toBeInTheDocument();
    const confirm = within(dialog).getByRole("button", { name: "Test and replace" });
    expect(confirm).toBeDisabled();

    fireEvent.change(within(dialog).getByLabelText("API key"), { target: { value: "new-secret-value" } });
    fireEvent.click(confirm);

    await waitFor(() => expect(rotateProviderConnectionSecret).toHaveBeenCalledWith(connection.connection_id, "new-secret-value"));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("requires an explicit confirmation before deleting a connection or model", async () => {
    render(<ModelCenterView language="en" />);
    fireEvent.click(await screen.findByRole("button", { name: /OpenAI/ }));

    const connectionCard = screen.getByText("Work account").closest("article");
    expect(connectionCard).not.toBeNull();
    fireEvent.click(within(connectionCard as HTMLElement).getByRole("button", { name: "Delete" }));
    const connectionDialog = screen.getByRole("dialog", { name: "Delete this connection?" });
    const cancel = within(connectionDialog).getByRole("button", { name: "Cancel" });
    expect(cancel).toHaveFocus();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(within(connectionCard as HTMLElement).getByRole("button", { name: "Delete" })).toHaveFocus());

    fireEvent.click(within(connectionCard as HTMLElement).getByRole("button", { name: "Delete" }));
    fireEvent.click(within(screen.getByRole("dialog", { name: "Delete this connection?" })).getByRole("button", { name: "Cancel" }));
    expect(deleteProviderConnection).not.toHaveBeenCalled();

    fireEvent.click(within(connectionCard as HTMLElement).getByRole("button", { name: "Delete" }));
    fireEvent.click(within(screen.getByRole("dialog", { name: "Delete this connection?" })).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(deleteProviderConnection).toHaveBeenCalledWith(connection.connection_id));

    fireEvent.click(screen.getByText("Work account"));
    const modelRow = await screen.findByText("Primary GPT").then((element) => element.closest("article"));
    expect(modelRow).not.toBeNull();
    fireEvent.click(within(modelRow as HTMLElement).getByRole("button", { name: "Delete" }));
    fireEvent.click(within(screen.getByRole("dialog", { name: "Delete this configured model?" })).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(deleteConfiguredModel).toHaveBeenCalledWith(model.configured_model_id));
  });

  it("blocks a remote custom endpoint until its full origin is acknowledged", async () => {
    render(<ModelCenterView language="en" />);
    fireEvent.click(await screen.findByRole("button", { name: /Custom endpoint/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save connection" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("full Origin");
    expect(saveProviderConnection).not.toHaveBeenCalled();

    fireEvent.click(screen.getByLabelText(/I confirm that the key and research content/));
    vi.mocked(saveProviderConnection).mockResolvedValue({
      ...connection, connection_id: "custom-test", provider_id: "custom",
      display_name: "Custom endpoint", endpoint: "https://models.example.test/v1", has_secret: false,
    });
    fireEvent.click(screen.getByRole("button", { name: "Save connection" }));
    await waitFor(() => expect(saveProviderConnection).toHaveBeenCalled());
  });

  it("saves an explicitly selected vision capability for a discovered model", async () => {
    vi.mocked(discoverConnectionModels).mockResolvedValue([{
      model_id: "vision-model", alias: "Vision Candidate", billing_class: "paid",
      capabilities: ["text_chat", "structured_json"],
    }]);
    render(<ModelCenterView language="en" />);
    fireEvent.click(await screen.findByRole("button", { name: /OpenAI/ }));
    fireEvent.click(screen.getByRole("button", { name: "Discover models" }));

    fireEvent.click(await screen.findByRole("checkbox", { name: "Select model: Vision Candidate" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Vision input: Vision Candidate" }));
    fireEvent.click(screen.getByRole("button", { name: "Add selected models" }));

    await waitFor(() => expect(saveConfiguredModel).toHaveBeenCalledWith(expect.objectContaining({
      model_id: "vision-model",
      capabilities: expect.arrayContaining(["text_chat", "structured_json", "vision"]),
    })));
  });
  it("shows a localized success message after testing a configured model", async () => {
    render(<ModelCenterView language="zh-CN" />);
    fireEvent.click(await screen.findByRole("button", { name: /OpenAI/ }));
    fireEvent.click(await screen.findByText("Work account"));
    fireEvent.click(screen.getByRole("button", { name: "测试" }));

    await waitFor(() => expect(testConfiguredModel).toHaveBeenCalledWith(model.configured_model_id));
    expect(await screen.findByText("连接测试成功。")).toBeVisible();
    expect(screen.queryByText("Connection succeeded.")).not.toBeInTheDocument();
  });

  it("shows built-in models immediately and exposes an explicit connection test", async () => {
    vi.mocked(listProviderConnections).mockResolvedValue([{ ...connection, status: "untested" }]);
    render(<ModelCenterView language="en" />);
    fireEvent.click(await screen.findByRole("button", { name: /OpenAI/ }));
    fireEvent.click(screen.getByText("Work account"));
    expect(await screen.findByText("GPT preset")).toBeVisible();
    expect(screen.getByText("Uses: gpt-test")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Test connection · Uses: gpt-test" }));
    await waitFor(() => expect(testProviderConnection).toHaveBeenCalledWith(connection.connection_id));
    expect(saveConfiguredModel).not.toHaveBeenCalled();
  });

  it("keeps unrelated safe controls interactive while a connection test is pending", async () => {
    let resolveTest!: (value: { ok: boolean; message: string }) => void;
    vi.mocked(testProviderConnection).mockImplementation(() => new Promise((resolve) => {
      resolveTest = resolve;
    }));
    render(<ModelCenterView language="en" />);
    fireEvent.click(await screen.findByRole("button", { name: /OpenAI/ }));

    const testButton = screen.getByRole("button", { name: "Test connection · Uses: gpt-test" });
    fireEvent.click(testButton);
    await waitFor(() => expect(testButton).toBeDisabled());
    expect(testButton).toHaveTextContent("Testing connection…");

    const safeFilter = screen.getByRole("button", { name: "Cloud" });
    expect(safeFilter).not.toBeDisabled();
    fireEvent.click(safeFilter);
    expect(safeFilter).toHaveAttribute("data-active", "true");

    resolveTest({ ok: true, message: "Connection succeeded." });
    await waitFor(() => expect(testProviderConnection).toHaveBeenCalledWith(connection.connection_id));
  });

  it("keeps custom connection testing available when a configured model exists", async () => {
    vi.mocked(listProviderConnections).mockResolvedValue([{ ...connection, provider_id: "custom" }]);
    vi.mocked(listConfiguredModels).mockResolvedValue([{ ...model, model_id: "custom-model" }]);
    render(<ModelCenterView language="en" />);
    fireEvent.click(await screen.findByRole("button", { name: /Custom endpoint/ }));
    fireEvent.click(screen.getByText("Work account"));
    fireEvent.click(screen.getByRole("button", { name: /Test connection/ }));
    await waitFor(() => expect(testProviderConnection).toHaveBeenCalledWith(connection.connection_id, "custom-model"));
  });

  it("keeps built-ins first and deduplicates online discovery results", async () => {
    vi.mocked(discoverConnectionModels).mockResolvedValue([
      { model_id: "gpt-online", alias: "Online model", billing_class: "paid", capabilities: ["text_chat", "structured_json"] },
      { model_id: "gpt-test", alias: "Duplicate online", billing_class: "paid", capabilities: ["text_chat", "structured_json"] },
    ]);
    render(<ModelCenterView language="en" />);
    fireEvent.click(await screen.findByRole("button", { name: /OpenAI/ }));
    fireEvent.click(screen.getByText("Work account"));
    fireEvent.click(screen.getByRole("button", { name: "Discover models" }));
    expect(await screen.findByText("GPT preset")).toBeVisible();
    expect(screen.getByText("Online model")).toBeVisible();
    expect(screen.queryByText("Duplicate online")).not.toBeInTheDocument();
  });
});

