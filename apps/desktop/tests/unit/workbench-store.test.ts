import { beforeEach, describe, expect, it } from "vitest";
import { useWorkbenchStore } from "@/lib/workbench/store";
import type { WorkbenchAgentConfigurationDraftTab, WorkbenchAgentTeamConfigurationDraftTab } from "@/lib/workbench/types";

const agentDraft = {
  agent_id: "reviewer",
  role: "代码审查",
  description: "审查代码质量",
  system_prompt: "请检查代码并给出结论。",
};

const teamDraft = {
  team_id: "review-team",
  name: "Review Team",
  description: "审查代码",
  max_runs: 8,
  start_node_id: "review",
  nodes: [{ node_id: "review", name: "审查", agent_id: "reviewer", statuses: [{ name: "done", color: "#16A34A" }] }],
  transitions: [{ from_node_id: "review", status: "done", target_node_id: "END" }],
  scope: "workspace" as const,
};

const session = (workspaceId: number) => useWorkbenchStore.getState().sessions[workspaceId];
const agentDraftTab = (workspaceId: number, toolCallId: string) => session(workspaceId).tabs
  .find((tab) => tab.id === `agent-config-draft:${toolCallId}`) as WorkbenchAgentConfigurationDraftTab;
const teamDraftTab = (workspaceId: number, toolCallId: string) => session(workspaceId).tabs
  .find((tab) => tab.id === `agent-team-config-draft:${toolCallId}`) as WorkbenchAgentTeamConfigurationDraftTab;

describe("workbench store", () => {
  beforeEach(() => useWorkbenchStore.setState({ sessions: {} }));

  it("按工作区分桶：切换工作区不影响另一个工作区的标签与草稿", () => {
    const store = useWorkbenchStore.getState();
    store.openAgent(7, { taskId: 21, title: "审查" });
    store.openAgent(7, { taskId: 21, title: "审查（更新）" });
    store.openAgentConfigurationDraft(7, { taskId: 20, toolCallId: "call-1", draft: agentDraft });
    agentDraftTab(7, "call-1").session.setField("description", "改过的描述");
    store.openFiles(8);

    expect(session(7).tabs.map((tab) => tab.id)).toEqual(["agent-task:21", "agent-config-draft:call-1"]);
    expect(session(7).tabs[0].title).toBe("审查（更新）");
    expect(agentDraftTab(7, "call-1").session.store.getState().form.description).toBe("改过的描述");
    expect(session(8).tabs.map((tab) => tab.id)).toEqual(["workspace-files"]);
  });

  it("关闭最后一个标签时收起面板，激活项落到相邻标签", () => {
    const store = useWorkbenchStore.getState();
    store.openAgent(7, { taskId: 1, title: "一" });
    store.openAgent(7, { taskId: 2, title: "二" });
    store.openAgent(7, { taskId: 3, title: "三" });
    store.activateTab(7, "agent-task:2");
    store.closeTab(7, "agent-task:2");
    expect(session(7).activeTabId).toBe("agent-task:3");
    store.closeTab(7, "agent-task:1");
    store.closeTab(7, "agent-task:3");
    expect(session(7)).toMatchObject({ tabs: [], activeTabId: null, panelOpen: false });
  });

  it("重复打开同一草稿不覆盖正在编辑的会话", () => {
    const store = useWorkbenchStore.getState();
    store.openAgentConfigurationDraft(7, { taskId: 20, toolCallId: "call-1", draft: agentDraft });
    const opened = agentDraftTab(7, "call-1").session;
    opened.setField("role", "资深审查");
    store.openAgent(7, { taskId: 21, title: "其他" });
    store.openAgentConfigurationDraft(7, { taskId: 20, toolCallId: "call-1", draft: agentDraft });

    expect(agentDraftTab(7, "call-1").session).toBe(opened);
    expect(session(7).activeTabId).toBe("agent-config-draft:call-1");
  });

  it("关闭已保存过的草稿记下已保存标记，重新打开时从原始草稿新建会话并清除标记", async () => {
    const store = useWorkbenchStore.getState();
    store.openAgentTeamConfigurationDraft(7, { taskId: 20, toolCallId: "team-1", draft: teamDraft });
    await teamDraftTab(7, "team-1").session.save(async () => teamDraft);
    store.closeTab(7, "agent-team-config-draft:team-1");
    expect(session(7).savedDraftIds).toEqual(["team-1"]);

    store.openAgentTeamConfigurationDraft(7, { taskId: 20, toolCallId: "team-1", draft: teamDraft });
    expect(session(7).savedDraftIds).toEqual([]);
    expect(teamDraftTab(7, "team-1").session.store.getState().dirty).toBe(true);
  });

  it("未保存过的草稿关闭后不记已保存标记", () => {
    const store = useWorkbenchStore.getState();
    store.openAgentConfigurationDraft(7, { taskId: 20, toolCallId: "call-1", draft: agentDraft });
    store.closeTab(7, "agent-config-draft:call-1");
    expect(session(7).savedDraftIds).toEqual([]);
  });

  it("删除 Task 时移除属于它的子 Agent 与草稿标签，删除工作区时丢弃整个桶", () => {
    const store = useWorkbenchStore.getState();
    store.openAgent(7, { taskId: 21, title: "子 Agent" });
    store.openAgentConfigurationDraft(7, { taskId: 20, toolCallId: "call-1", draft: agentDraft });
    store.openFiles(7);
    store.openAgent(8, { taskId: 31, title: "保留" });

    store.forgetTasks([20, 21]);
    expect(session(7).tabs.map((tab) => tab.id)).toEqual(["workspace-files"]);
    expect(session(8).tabs.map((tab) => tab.id)).toEqual(["agent-task:31"]);

    store.forgetWorkspace(7);
    expect(session(7)).toBeUndefined();
  });

  it("删除 Task 移除已保存过的草稿时不记已保存标记，未受影响的工作区桶保持原引用", async () => {
    const store = useWorkbenchStore.getState();
    store.openAgentTeamConfigurationDraft(7, { taskId: 20, toolCallId: "team-1", draft: teamDraft });
    await teamDraftTab(7, "team-1").session.save(async () => teamDraft);
    store.openAgent(8, { taskId: 31, title: "保留" });
    const untouched = session(8);

    store.forgetTasks([20]);
    expect(session(7).tabs).toEqual([]);
    expect(session(7).savedDraftIds).toEqual([]);
    expect(session(8)).toBe(untouched);
  });
});
