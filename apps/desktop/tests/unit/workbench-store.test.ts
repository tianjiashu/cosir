import { beforeEach, describe, expect, it } from "vitest";
import { useDockStore } from "@/lib/layout/dock-store";
import { useWorkbenchStore } from "@/lib/workbench/workbench-store";
import type { PersistAgent } from "@/lib/agent-editor/agent-editor-session";
import type { WorkbenchAgentConfigurationDraftTab } from "@/lib/workbench/workbench-tab-types";

const agentDraft = {
  agent_id: "reviewer",
  role: "代码审查",
  description: "审查代码质量",
  system_prompt: "请检查代码并给出结论。",
};

const persistAgent: PersistAgent = async (input) => ({
  ...input, source: "user_file", path: null, editable: true, deletable: true,
  validation_status: "valid", validation_error: null, file_name: null,
});

const workbench = (workspaceId: number) => useWorkbenchStore.getState().byWorkspace[workspaceId];
const agentDraftTab = (workspaceId: number, toolCallId: string) => workbench(workspaceId).tabs
  .find((tab) => tab.id === `agent-config-draft:20:${toolCallId}`) as WorkbenchAgentConfigurationDraftTab;

describe("workbench store", () => {
  beforeEach(() => useWorkbenchStore.setState({ byWorkspace: {} }));

  it("按工作区分桶：切换工作区不影响另一个工作区的标签与草稿", () => {
    const store = useWorkbenchStore.getState();
    store.openAgent(7, { sessionId: 21, title: "审查" });
    store.openAgent(7, { sessionId: 21, title: "审查（更新）" });
    store.openAgentConfigurationDraft(7, { sessionId: 20, toolCallId: "call-1", draft: agentDraft });
    agentDraftTab(7, "call-1").session.setField("description", "改过的描述");
    store.openAgent(8, { sessionId: 30, title: "另一个工作区" });

    expect(workbench(7).tabs.map((tab) => tab.id)).toEqual(["agent-session:21", "agent-config-draft:20:call-1"]);
    expect(workbench(7).tabs[0].title).toBe("审查（更新）");
    expect(agentDraftTab(7, "call-1").session.store.getState().form.description).toBe("改过的描述");
    expect(workbench(8).tabs.map((tab) => tab.id)).toEqual(["agent-session:30"]);
  });

  it("关闭标签时激活项落到相邻标签；关完后没有标签（面板随之不显示，见 useDockPanels）", () => {
    const store = useWorkbenchStore.getState();
    store.openAgent(7, { sessionId: 1, title: "一" });
    store.openAgent(7, { sessionId: 2, title: "二" });
    store.openAgent(7, { sessionId: 3, title: "三" });
    store.activateTab(7, "agent-session:2");
    store.closeTab(7, "agent-session:2");
    expect(workbench(7).activeTabId).toBe("agent-session:3");
    store.closeTab(7, "agent-session:1");
    store.closeTab(7, "agent-session:3");
    expect(workbench(7)).toMatchObject({ tabs: [], activeTabId: null });
  });

  it("重复打开同一草稿不覆盖正在编辑的会话", () => {
    const store = useWorkbenchStore.getState();
    store.openAgentConfigurationDraft(7, { sessionId: 20, toolCallId: "call-1", draft: agentDraft });
    const opened = agentDraftTab(7, "call-1").session;
    opened.setField("role", "资深审查");
    store.openAgent(7, { sessionId: 21, title: "其他" });
    store.openAgentConfigurationDraft(7, { sessionId: 20, toolCallId: "call-1", draft: agentDraft });

    expect(agentDraftTab(7, "call-1").session).toBe(opened);
    expect(workbench(7).activeTabId).toBe("agent-config-draft:20:call-1");
  });

  it("关闭已保存过的草稿记下已保存标记，重新打开时从原始草稿新建会话并清除标记", async () => {
    const store = useWorkbenchStore.getState();
    store.openAgentConfigurationDraft(7, { sessionId: 20, toolCallId: "call-1", draft: agentDraft });
    await agentDraftTab(7, "call-1").session.save(persistAgent);
    store.closeTab(7, "agent-config-draft:20:call-1");
    expect(workbench(7).savedDraftIds).toEqual(["20:call-1"]);

    store.openAgentConfigurationDraft(7, { sessionId: 20, toolCallId: "call-1", draft: agentDraft });
    expect(workbench(7).savedDraftIds).toEqual([]);
    expect(agentDraftTab(7, "call-1").session.store.getState().dirty).toBe(true);
  });

  it("两个 Session 的同号调用各开各的草稿标签", () => {
    const store = useWorkbenchStore.getState();
    store.openAgentConfigurationDraft(7, { sessionId: 20, toolCallId: "call_0", draft: agentDraft });
    store.openAgentConfigurationDraft(7, { sessionId: 21, toolCallId: "call_0", draft: agentDraft });
    expect(workbench(7).tabs.map((tab) => tab.id)).toEqual(["agent-config-draft:20:call_0", "agent-config-draft:21:call_0"]);
  });

  it("未保存过的草稿关闭后不记已保存标记", () => {
    const store = useWorkbenchStore.getState();
    store.openAgentConfigurationDraft(7, { sessionId: 20, toolCallId: "call-1", draft: agentDraft });
    store.closeTab(7, "agent-config-draft:20:call-1");
    expect(workbench(7).savedDraftIds).toEqual([]);
  });

  it("删除 Session 时移除属于它的子 Agent 与草稿标签，删除工作区时丢弃整个桶", () => {
    const store = useWorkbenchStore.getState();
    store.openAgent(7, { sessionId: 21, title: "子 Agent" });
    store.openAgentConfigurationDraft(7, { sessionId: 20, toolCallId: "call-1", draft: agentDraft });
    store.openAgent(7, { sessionId: 22, title: "保留" });
    store.openAgent(8, { sessionId: 31, title: "保留" });

    store.forgetSessions([20, 21]);
    expect(workbench(7).tabs.map((tab) => tab.id)).toEqual(["agent-session:22"]);
    expect(workbench(8).tabs.map((tab) => tab.id)).toEqual(["agent-session:31"]);

    store.forgetWorkspace(7);
    expect(workbench(7)).toBeUndefined();
  });

  it("删除 Session 移除已保存过的草稿时不记已保存标记，未受影响的工作区桶保持原引用", async () => {
    const store = useWorkbenchStore.getState();
    store.openAgentConfigurationDraft(7, { sessionId: 20, toolCallId: "call-1", draft: agentDraft });
    await agentDraftTab(7, "call-1").session.save(persistAgent);
    store.openAgent(8, { sessionId: 31, title: "保留" });
    const untouched = workbench(8);

    store.forgetSessions([20]);
    expect(workbench(7).tabs).toEqual([]);
    expect(workbench(7).savedDraftIds).toEqual([]);
    expect(workbench(8)).toBe(untouched);
  });

  // 目的：打开标签就是要看它；潜在缺陷：标签加进去了，停靠区却还停在文件或浏览器面板。
  it("打开标签把停靠区切到工作台，激活与关闭不改动停靠区", () => {
    useDockStore.getState().show("browser");
    useWorkbenchStore.getState().openAgent(7, { sessionId: 21, title: "子会话" });
    expect(useDockStore.getState().panel).toBe("workbench");
    useDockStore.getState().show("files");
    useWorkbenchStore.getState().closeTab(7, "agent-session:21");
    expect(useDockStore.getState().panel).toBe("files");
  });
});
