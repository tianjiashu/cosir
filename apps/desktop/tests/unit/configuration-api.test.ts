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
      provider_id: null,
      model_name: null,
      model_settings: { temperature: 0.2 },
    });

    expect(requestJson).toHaveBeenCalledWith(
      "/configuration/agents/reviewer",
      expect.objectContaining({ method: "PUT" }),
    );
    expect(jsonRequestInit).toHaveBeenCalledWith(
      expect.objectContaining({ provider_id: null, model_name: null }),
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
});
