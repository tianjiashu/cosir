import { describe, expect, it } from "vitest";
import { useWorkbenchStore } from "@/lib/workbench/store";

describe("workbench store", () => {
  it("deduplicates child Agent tabs and isolates workspaces", () => {
    const store = useWorkbenchStore.getState();
    store.ensureWorkspace(7);
    store.openAgentTab({ workspaceId: 7, taskId: 21, title: "审查", role: "Reviewer" });
    store.openAgentTab({ workspaceId: 7, taskId: 21, title: "审查（更新）", role: "Reviewer" });
    expect(useWorkbenchStore.getState().tabs).toHaveLength(1);
    expect(useWorkbenchStore.getState().activeTabId).toBe("agent-task:21");
    expect(useWorkbenchStore.getState().tabs[0].title).toBe("审查（更新）");

    store.ensureWorkspace(8);
    expect(useWorkbenchStore.getState().tabs).toEqual([]);
    expect(useWorkbenchStore.getState().activeTabId).toBeNull();
    store.ensureWorkspace(null);
  });

  it("closes a child tab without changing backend session state", () => {
    const store = useWorkbenchStore.getState();
    store.ensureWorkspace(7);
    store.openAgentTab({ workspaceId: 7, taskId: 501, title: "审查", role: "Reviewer" });
    store.closeTab("agent-task:501");

    expect(useWorkbenchStore.getState().tabs).toEqual([]);
    expect(useWorkbenchStore.getState().panelOpen).toBe(false);
    store.ensureWorkspace(null);
  });

  it("keeps an edited configuration draft when switching workbench tabs", () => {
    const store = useWorkbenchStore.getState();
    const draft = {
      agent_id: "reviewer",
      role: "代码审查",
      description: "审查代码质量",
      system_prompt: "请检查代码并给出结论。",
      allowed_tool_groups: [],
      max_steps: 100,
      model_config_id: null,
      model_settings: {},
    };
    store.ensureWorkspace(9);
    store.openAgentConfigurationDraftTab({
      workspaceId: 9,
      taskId: 601,
      toolCallId: "tool-call-601",
      title: "reviewer",
      draft,
      scope: "workspace",
    });
    store.updateAgentConfigurationDraft("agent-config-draft:tool-call-601", {
      ...draft,
      description: "审查 TypeScript 代码质量",
    });
    store.setDraftDirty("agent-config-draft:tool-call-601", false);
    store.openAgentTab({ workspaceId: 9, taskId: 602, title: "另一个 Agent", role: "Tester" });
    store.activateTab("agent-config-draft:tool-call-601");

    const draftTab = useWorkbenchStore.getState().tabs.find(
      (tab) => tab.id === "agent-config-draft:tool-call-601",
    );
    expect(draftTab?.kind).toBe("agent-configuration-draft");
    expect(draftTab?.kind === "agent-configuration-draft" && draftTab.draft.description).toBe("审查 TypeScript 代码质量");
    expect(draftTab?.kind === "agent-configuration-draft" && draftTab.dirty).toBe(false);
    store.ensureWorkspace(null);
  });

  it("tracks saved draft card state without persistence", () => {
    const store = useWorkbenchStore.getState();
    store.ensureWorkspace(10);
    store.openAgentConfigurationDraftTab({
      workspaceId: 10,
      taskId: 701,
      toolCallId: "tool-call-701",
      title: "tester",
      draft: {
        agent_id: "tester",
        role: "测试",
        description: "运行测试",
        system_prompt: "请运行测试。",
        allowed_tool_groups: [],
        max_steps: 100,
        model_config_id: null,
        model_settings: {},
      },
      scope: "workspace",
    });
    store.setDraftDirty("agent-config-draft:tool-call-701", false);
    expect(useWorkbenchStore.getState().savedDraftToolCallIds).toContain("tool-call-701");
    store.ensureWorkspace(null);
  });

  it("remembers the saved Team draft scope so later edits update the existing file", () => {
    const store = useWorkbenchStore.getState();
    store.ensureWorkspace(11);
    const draft = {
      team_id: "review-team",
      name: "Review Team",
      description: "审查代码",
      max_runs: 8,
      start_node_id: "review",
      nodes: [{ node_id: "review", name: "审查", agent_id: "reviewer", statuses: ["done"] }],
      transitions: [{ from_node_id: "review", status: "done", target_node_id: "END" }],
      scope: "workspace" as const,
    };
    store.openAgentTeamConfigurationDraftTab({
      workspaceId: 11,
      taskId: 801,
      toolCallId: "team-tool-call-801",
      title: draft.name,
      draft,
    });
    store.markAgentTeamDraftSaved("agent-team-config-draft:team-tool-call-801", "workspace");
    store.activateTab("agent-task:801");
    store.openAgentTeamConfigurationDraftTab({
      workspaceId: 11,
      taskId: 801,
      toolCallId: "team-tool-call-801",
      title: draft.name,
      draft,
    });

    const tab = useWorkbenchStore.getState().tabs.find(
      (candidate) => candidate.id === "agent-team-config-draft:team-tool-call-801",
    );
    expect(tab?.kind === "agent-team-configuration-draft" && tab.savedScope).toBe("workspace");
    expect(tab?.kind === "agent-team-configuration-draft" && tab.dirty).toBe(false);
    expect(useWorkbenchStore.getState().savedDraftToolCallIds).toContain("team-tool-call-801");
    store.ensureWorkspace(null);
  });

  it("does not overwrite an open draft and resets saved state when reopening it", () => {
    const store = useWorkbenchStore.getState();
    const draft = {
      agent_id: "writer",
      role: "文档",
      description: "编写文档",
      system_prompt: "请编写文档。",
      allowed_tool_groups: [],
      max_steps: 100,
      model_config_id: null,
      model_settings: {},
    };
    store.ensureWorkspace(11);
    store.openAgentConfigurationDraftTab({
      workspaceId: 11,
      taskId: 801,
      toolCallId: "tool-call-801",
      title: "writer",
      draft,
      scope: "workspace",
    });
    store.updateAgentConfigurationDraft("agent-config-draft:tool-call-801", { ...draft, description: "编写技术文档" });
    store.openAgentConfigurationDraftTab({
      workspaceId: 11,
      taskId: 801,
      toolCallId: "tool-call-801",
      title: "writer",
      draft,
      scope: "workspace",
    });
    let tab = useWorkbenchStore.getState().tabs.find((item) => item.id === "agent-config-draft:tool-call-801");
    expect(tab?.kind === "agent-configuration-draft" && tab.draft.description).toBe("编写技术文档");

    store.setDraftDirty("agent-config-draft:tool-call-801", false);
    store.closeTab("agent-config-draft:tool-call-801");
    store.openAgentConfigurationDraftTab({
      workspaceId: 11,
      taskId: 801,
      toolCallId: "tool-call-801",
      title: "writer",
      draft,
      scope: "workspace",
    });
    tab = useWorkbenchStore.getState().tabs.find((item) => item.id === "agent-config-draft:tool-call-801");
    expect(tab?.kind === "agent-configuration-draft" && tab.dirty).toBe(true);
    expect(useWorkbenchStore.getState().savedDraftToolCallIds).not.toContain("tool-call-801");
    store.ensureWorkspace(null);
  });
});
