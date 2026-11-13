import { describe, expect, it, vi } from "vitest";

const requestJson = vi.hoisted(() => vi.fn());
const jsonRequestInit = vi.hoisted(() => vi.fn((body: unknown, init: RequestInit) => ({ ...init, body })));

vi.mock("@/lib/http/client", () => ({ requestJson, jsonRequestInit }));

describe("configuration API", () => {
  it("routes sub-Agent CRUD to the system or the workspace collection", async () => {
    requestJson.mockResolvedValue({ agent_id: "reviewer" });
    const {
      createAgentConfiguration,
      deleteAgentConfiguration,
      getAgentConfigurations,
      updateAgentConfiguration,
    } = await import("@/lib/api/configuration");
    const input = {
      agent_id: "reviewer",
      role: "child",
      description: "review",
      system_prompt: "read only",
      allowed_tool_groups: ["文件"],
      max_steps: 100,
      model_config_id: null,
      model_settings: {},
    };

    await getAgentConfigurations(null);
    expect(requestJson).toHaveBeenLastCalledWith("/configuration/agents", undefined);
    await getAgentConfigurations(3);
    expect(requestJson).toHaveBeenLastCalledWith("/workspaces/3/configuration/agents", undefined);
    await createAgentConfiguration(null, input);
    expect(requestJson).toHaveBeenLastCalledWith("/configuration/agents", expect.objectContaining({ method: "POST" }));
    await createAgentConfiguration(3, input);
    expect(requestJson).toHaveBeenLastCalledWith("/workspaces/3/configuration/agents", expect.objectContaining({ method: "POST" }));
    await updateAgentConfiguration(3, "a/b", input);
    expect(requestJson).toHaveBeenLastCalledWith("/workspaces/3/configuration/agents/a%2Fb", expect.objectContaining({ method: "PUT" }));
    await deleteAgentConfiguration(null, "reviewer");
    expect(requestJson).toHaveBeenLastCalledWith("/configuration/agents/reviewer", { method: "DELETE" });
    await deleteAgentConfiguration(3, "reviewer");
    expect(requestJson).toHaveBeenLastCalledWith("/workspaces/3/configuration/agents/reviewer", { method: "DELETE" });
  });

  it("sends agent model overrides without configuration version metadata", async () => {
    jsonRequestInit.mockClear();
    requestJson.mockResolvedValueOnce({ agent_id: "reviewer" });
    const { updateAgentConfiguration } = await import("@/lib/api/configuration");
    await updateAgentConfiguration(null, "reviewer", {
      agent_id: "reviewer",
      role: "child",
      description: "review",
      system_prompt: "read only",
      allowed_tool_groups: ["文件"],
      max_steps: 100,
      model_config_id: null,
      model_settings: { temperature: 0.2 },
    });

    expect(requestJson).toHaveBeenCalledWith(
      "/configuration/agents/reviewer",
      expect.objectContaining({ method: "PUT" }),
    );
    expect(jsonRequestInit).toHaveBeenCalledWith(
      expect.objectContaining({ model_config_id: null }),
      { method: "PUT" },
    );
    expect(jsonRequestInit.mock.calls[0][0]).not.toHaveProperty("version");
    expect(jsonRequestInit.mock.calls[0][0]).not.toHaveProperty("expected_version");
  });

  it("preserves clear/unchanged environment operations without version metadata", async () => {
    requestJson.mockResolvedValueOnce({ groups: [] });
    const { updateEnvironmentConfiguration } = await import("@/lib/api/configuration");
    await updateEnvironmentConfiguration({
      FIRECRAWL_API_KEY: { operation: "unchanged" },
      DEFAULT_LANGUAGE: { operation: "clear" },
    });

    expect(jsonRequestInit).toHaveBeenCalledWith(
      {
        changes: {
          FIRECRAWL_API_KEY: { operation: "unchanged" },
          DEFAULT_LANGUAGE: { operation: "clear" },
        },
      },
      { method: "PUT" },
    );
  });
});
