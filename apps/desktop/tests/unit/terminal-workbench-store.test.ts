import { beforeEach, describe, expect, it } from "vitest";

import { selectTerminalTask, useTerminalWorkbenchStore } from "@/lib/terminal/terminal-workbench-store";
import { endedTerminalSessions, mountedTerminalSessionIds, terminalBadge, terminalTabs } from "@/lib/terminal/terminal-sessions";
import { terminalSession } from "./transport-fixtures";

const TASK = 42;
const task = () => selectTerminalTask(TASK)(useTerminalWorkbenchStore.getState());

describe("终端工作台 store", () => {
  beforeEach(() => useTerminalWorkbenchStore.setState({ tasks: {} }));

  it("按 Task 独立记住面板开关与选中会话", () => {
    const store = useTerminalWorkbenchStore.getState();
    store.syncSessions(TASK, [terminalSession("t1"), terminalSession("t2")]);
    store.select(TASK, "t2");
    store.setOpen(TASK, true);
    store.syncSessions(7, [terminalSession("x1")]);

    expect(task()).toMatchObject({ open: true, activeSessionId: "t2" });
    expect(selectTerminalTask(7)(useTerminalWorkbenchStore.getState())).toMatchObject({ open: false, activeSessionId: "x1" });
    expect(terminalBadge(task().sessions, task().acknowledgedEnds).count).toBe(2);
  });

  it("页头角标：未看过的异常结束提醒，打开面板即视为看过", () => {
    const store = useTerminalWorkbenchStore.getState();
    const badge = () => terminalBadge(task().sessions, task().acknowledgedEnds);
    store.syncSessions(TASK, [terminalSession("t1"), terminalSession("t2")]);
    expect(badge()).toMatchObject({ count: 2, tone: null, hint: null });

    // 收起时异常退出：标红；正常退出（0）与主动关闭不提醒。
    store.syncSessions(TASK, [
      terminalSession("t1"),
      terminalSession("t2", { status: "exited", exitCode: 2 }),
      terminalSession("t3", { status: "closed" }),
      terminalSession("t4", { status: "exited", exitCode: 0 }),
    ]);
    expect(badge()).toMatchObject({ count: 1, tone: "attention" });
    // 已结束会话不在标签栏里，仍按编号称呼，与用户看到过的标签名一致。
    expect(badge().hint).toContain("「终端 2」异常结束（退出码 2）");

    store.setOpen(TASK, true);
    expect(badge().tone).toBeNull();
    // 面板开着时结束的会话用户看得见，收起后也不再提醒。
    store.syncSessions(TASK, [terminalSession("t1", { status: "failed" }), terminalSession("t2", { status: "exited", exitCode: 2 })]);
    store.setOpen(TASK, false);
    expect(badge()).toEqual({ count: 0, tone: null, hint: null });
  });

  it("Task 删除后 forget 丢弃其桶，其他 Task 不受影响", () => {
    const store = useTerminalWorkbenchStore.getState();
    store.syncSessions(TASK, [terminalSession("t1")]);
    store.syncSessions(7, [terminalSession("x1")]);
    const before = useTerminalWorkbenchStore.getState();
    store.forget([99]);
    expect(useTerminalWorkbenchStore.getState()).toBe(before);
    store.forget([TASK]);
    expect(TASK in useTerminalWorkbenchStore.getState().tasks).toBe(false);
    expect(useTerminalWorkbenchStore.getState().tasks[7]?.activeSessionId).toBe("x1");
  });

  it("离开 Task 页清空会话镜像但保留面板偏好；已 forget 的 Task 不被重建", () => {
    const store = useTerminalWorkbenchStore.getState();
    store.syncSessions(TASK, [terminalSession("t1"), terminalSession("t2")]);
    store.select(TASK, "t2");
    store.setOpen(TASK, true);
    store.syncSessions(TASK, null);
    expect(task()).toMatchObject({ sessions: null, open: true, activeSessionId: "t2" });
    expect(terminalBadge(task().sessions, task().acknowledgedEnds).count).toBe(0);

    store.forget([TASK]);
    store.syncSessions(TASK, null);
    expect(TASK in useTerminalWorkbenchStore.getState().tasks).toBe(false);
  });

  it("没有活跃会话时打开面板才标记自动新建；会话列表未知时也标记，等列表到达再判定", () => {
    const store = useTerminalWorkbenchStore.getState();
    store.toggle(TASK);
    expect(task()).toMatchObject({ open: true, autoCreate: true, sessions: null });
    store.consumeAutoCreate(TASK);
    store.toggle(TASK);
    store.syncSessions(TASK, [terminalSession("t1")]);
    store.toggle(TASK);
    expect(task()).toMatchObject({ open: true, autoCreate: false });
  });

  it("交接从无到有时自动打开面板并聚焦该会话；Agent 新开会话不弹出", () => {
    const store = useTerminalWorkbenchStore.getState();
    store.syncSessions(TASK, [terminalSession("t1")]);
    store.syncSessions(TASK, [terminalSession("t1"), terminalSession("a1", { createdBy: "agent" })]);
    expect(task().open).toBe(false);

    const before = task().focusToken;
    store.syncSessions(TASK, [terminalSession("t1"), terminalSession("a1", { createdBy: "agent", handoff: { reason: "输入密码", toolCallId: "call-handoff" } })]);
    expect(task()).toMatchObject({ open: true, activeSessionId: "a1" });
    expect(task().focusToken).toBe(before + 1);

    // 同一交接持续存在不重复抢焦点；用户关了面板也不会被再次弹开。
    store.setOpen(TASK, false);
    store.syncSessions(TASK, [terminalSession("t1"), terminalSession("a1", { createdBy: "agent", handoff: { reason: "输入密码", toolCallId: "call-handoff" } })]);
    expect(task().open).toBe(false);
  });

  it("选中的会话从 snapshot 消失后退到第一个活跃会话；已结束但仍在列表里的保留选中", () => {
    const store = useTerminalWorkbenchStore.getState();
    store.syncSessions(TASK, [terminalSession("t1"), terminalSession("t2")]);
    store.select(TASK, "t2");
    store.syncSessions(TASK, [terminalSession("t1"), terminalSession("t2", { status: "exited", exitCode: 0 })]);
    expect(task().activeSessionId).toBe("t2");
    store.syncSessions(TASK, [terminalSession("t1")]);
    expect(task().activeSessionId).toBe("t1");
    store.syncSessions(TASK, []);
    expect(task().activeSessionId).toBeNull();
  });

  it("reveal 打开面板、选中会话并请求焦点", () => {
    const store = useTerminalWorkbenchStore.getState();
    store.syncSessions(TASK, [terminalSession("t1"), terminalSession("t2")]);
    store.reveal(TASK, "t2");
    expect(task()).toMatchObject({ open: true, activeSessionId: "t2", autoCreate: false, focusToken: 1 });
  });
});

describe("终端标签命名", () => {
  it("单会话显示「终端」不编号；多会话按序编号，Agent label 优先", () => {
    expect(terminalTabs([terminalSession("t1")], "t1")).toEqual([
      expect.objectContaining({ title: "终端" }),
    ]);
    const tabs = terminalTabs([
      terminalSession("t1"),
      terminalSession("a1", { createdBy: "agent", label: "dev server" }),
      terminalSession("t3"),
    ], "t1");
    expect(tabs.map((tab) => tab.title)).toEqual(["终端 1", "dev server", "终端 3"]);
  });

  it("编号取会话在完整列表中的序号：关掉前面的会话不让后面的标签改名", () => {
    const tabs = terminalTabs([
      terminalSession("t1", { status: "closed" }),
      terminalSession("t2"),
      terminalSession("t3"),
    ], "t2");
    expect(tabs.map((tab) => tab.title)).toEqual(["终端 2", "终端 3"]);
  });

  it("已结束会话收进「已结束」，只有当前选中的那个留在标签栏", () => {
    const sessions = [terminalSession("t1"), terminalSession("old", { status: "closed" })];
    expect(terminalTabs(sessions, "t1").map((tab) => tab.session.sessionId)).toEqual(["t1"]);
    expect(terminalTabs(sessions, "old").map((tab) => tab.session.sessionId)).toEqual(["t1", "old"]);
    expect(endedTerminalSessions(sessions).map((session) => session.sessionId)).toEqual(["old"]);
  });
});

describe("终端视图挂载规则", () => {
  const running = (id: string) => terminalSession(id);
  const ended = (id: string) => terminalSession(id, { status: "exited", exitCode: 0 });

  it("活跃会话在面板可见且选中时挂载，之后切走、收起面板都常驻", () => {
    const sessions = [running("t1"), running("t2")];
    expect(mountedTerminalSessionIds([], sessions, "t1", false)).toEqual([]);
    const first = mountedTerminalSessionIds([], sessions, "t1", true);
    expect(first).toEqual(["t1"]);
    const both = mountedTerminalSessionIds(first, sessions, "t2", true);
    expect(both).toEqual(["t1", "t2"]);
    expect(mountedTerminalSessionIds(both, sessions, "t1", false)).toBe(both);
  });

  it("已结束会话只在被选中查看时挂载，选中别的会话即释放", () => {
    const sessions = [running("t1"), ended("old")];
    const viewing = mountedTerminalSessionIds(["t1"], sessions, "old", true);
    expect(viewing).toEqual(["t1", "old"]);
    expect(mountedTerminalSessionIds(viewing, sessions, "t1", true)).toEqual(["t1"]);
  });

  it("正在看的会话退出后保留到切走；未选中的会话结束即释放", () => {
    const mounted = ["t1", "t2"];
    const sessions = [ended("t1"), ended("t2")];
    expect(mountedTerminalSessionIds(mounted, sessions, "t1", true)).toEqual(["t1"]);
  });

  it("会话从 snapshot 消失（后端重启、超出保留数）时释放", () => {
    expect(mountedTerminalSessionIds(["t1", "t2"], [running("t2")], "t2", true)).toEqual(["t2"]);
    expect(mountedTerminalSessionIds(["t1"], [], null, true)).toEqual([]);
  });
});
