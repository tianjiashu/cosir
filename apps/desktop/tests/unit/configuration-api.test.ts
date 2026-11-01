import { describe, expect, it, vi } from "vitest";

const requestJson = vi.hoisted(() => vi.fn());
const jsonRequestInit = vi.hoisted(() => vi.fn((body: unknown, init: RequestInit) => ({ ...init, body })));

vi.mock("@/lib/http/client", () => ({ requestJson, jsonRequestInit }));

describe("configuration API", () => {
  it("routes Team CRUD by owner and derives both body scopes from it", async () => {
    requestJson.mockResolvedValue({ team_id: "review-team" });
    const {
      createAgentTeamConfiguration,
      deleteAgentTeamConfiguration,
      getAgentTeamConfigurations,
      updateAgentTeamConfiguration,
    } = await import("@/lib/api/agent-teams");
    const configuration = {
      team_id: "review-team",
      name: "Review Team",
      description: "Review changes",
      max_runs: 8,
      start_node_id: "review",
      nodes: [{ node_id: "review", name: "Review", agent_id: "reviewer", statuses: [{ name: "done", color: "#16A34A" }] }],
      transitions: [{ from_node_id: "review", status: "done", target_node_id: "END" }],
      scope: "workspace" as const,
    };

    await createAgentTeamConfiguration(3, configuration);
    expect(requestJson).toHaveBeenLastCalledWith(
      "/workspaces/3/configuration/agent-teams",
      expect.objectContaining({ method: "POST" }),
    );
    expect(jsonRequestInit).toHaveBeenLastCalledWith({ scope: "workspace", configuration }, { method: "POST" });

    await updateAgentTeamConfiguration(null, "review-team", configuration);
    expect(requestJson).toHaveBeenLastCalledWith(
      "/configuration/agent-teams/review-team",
      expect.objectContaining({ method: "PUT" }),
    );
    expect(jsonRequestInit).toHaveBeenLastCalledWith(
      { scope: "system", configuration: { ...configuration, scope: "system" } },
      { method: "PUT" },
    );

    await updateAgentTeamConfiguration(3, "review-team", configuration);
    expect(requestJson).toHaveBeenLastCalledWith(
      "/workspaces/3/configuration/agent-teams/review-team",
      expect.objectContaining({ method: "PUT" }),
    );
    await deleteAgentTeamConfiguration(3, "review-team");
    expect(requestJson).toHaveBeenLastCalledWith("/workspaces/3/configuration/agent-teams/review-team", { method: "DELETE" });
    await deleteAgentTeamConfiguration(null, "review-team");
    expect(requestJson).toHaveBeenLastCalledWith("/configuration/agent-teams/review-team", { method: "DELETE" });
    await getAgentTeamConfigurations(null);
    expect(requestJson).toHaveBeenLastCalledWith("/configuration/agent-teams", undefined);
    await getAgentTeamConfigurations(3);
    expect(requestJson).toHaveBeenLastCalledWith("/workspaces/3/configuration/agent-teams", undefined);
  });

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

  it("reads and replaces the terminal deny-list pattern array", async () => {
    requestJson.mockResolvedValueOnce({ patterns: ["\\brm\\b"] });
    const {
      getTerminalDenylistConfiguration,
      updateTerminalDenylistConfiguration,
    } = await import("@/lib/api/configuration");

    await getTerminalDenylistConfiguration();
    expect(requestJson).toHaveBeenCalledWith("/configuration/terminal-denylist", undefined);

    await updateTerminalDenylistConfiguration(["\\bmkfs\\b"]);
    expect(requestJson).toHaveBeenCalledWith(
      "/configuration/terminal-denylist",
      expect.objectContaining({ method: "PUT" }),
    );
    expect(jsonRequestInit).toHaveBeenCalledWith(
      { patterns: ["\\bmkfs\\b"] },
      { method: "PUT" },
    );
  });
});
