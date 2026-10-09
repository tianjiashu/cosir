import { describe, expect, it, vi } from "vitest";

const requestJson = vi.hoisted(() => vi.fn());
const jsonRequestInit = vi.hoisted(() => vi.fn((body: unknown, init: RequestInit) => ({ ...init, body })));

vi.mock("@/lib/http/client", () => ({ requestJson, jsonRequestInit }));

describe("configuration API", () => {
  it("expands selected tool groups and restores only complete groups", async () => {
    const { toolGroupsForNames, toolNamesForGroups } = await import("@/lib/api/tools");
    const groups = [
      { group: "文件", tools: [{ name: "read_file", description: "读取" }, { name: "write_file", description: "写入" }] },
      { group: "联网", tools: [{ name: "web_search", description: "搜索" }] },
    ];

    expect(toolNamesForGroups(["联网", "文件"], groups)).toEqual(["read_file", "write_file", "web_search"]);
    expect(toolGroupsForNames(["read_file", "write_file", "unknown"], groups)).toEqual(["文件"]);
  });

  it("routes Team CRUD through the explicit system or workspace scope", async () => {
    requestJson.mockResolvedValue({ team_id: "review-team" });
    const {
      createAgentTeamConfiguration,
      deleteAgentTeamConfiguration,
      updateAgentTeamConfiguration,
    } = await import("@/lib/api/agent-teams");
    const configuration = {
      team_id: "review-team",
      name: "Review Team",
      description: "Review changes",
      max_runs: 8,
      start_node_id: "review",
      nodes: [{ node_id: "review", name: "Review", agent_id: "reviewer", statuses: ["done"] }],
      transitions: [{ from_node_id: "review", status: "done", target_node_id: "END" }],
      scope: "workspace" as const,
    };

    await createAgentTeamConfiguration({ scope: "workspace", configuration }, 3);
    expect(requestJson).toHaveBeenCalledWith(
      "/workspaces/3/configuration/agent-teams",
      expect.objectContaining({ method: "POST" }),
    );
    await updateAgentTeamConfiguration("review-team", {
      scope: "system",
      configuration: { ...configuration, scope: "system" },
    });
    expect(requestJson).toHaveBeenCalledWith(
      "/configuration/agent-teams/review-team",
      expect.objectContaining({ method: "PUT" }),
    );
    await deleteAgentTeamConfiguration("review-team", "workspace", 3);
    expect(requestJson).toHaveBeenCalledWith(
      "/workspaces/3/configuration/agent-teams/review-team",
      { method: "DELETE" },
    );
  });

  it("sends agent model overrides without configuration version metadata", async () => {
    requestJson.mockResolvedValueOnce({ agent_id: "reviewer" });
    const { updateAgentConfiguration } = await import("@/lib/api/configuration");
    await updateAgentConfiguration("reviewer", {
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
      LANGFUSE_SECRET_KEY: { operation: "unchanged" },
      DEFAULT_LANGUAGE: { operation: "clear" },
    });

    expect(jsonRequestInit).toHaveBeenCalledWith(
      {
        changes: {
          LANGFUSE_SECRET_KEY: { operation: "unchanged" },
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
    expect(requestJson).toHaveBeenCalledWith("/configuration/terminal-denylist");

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
