import { beforeEach, describe, expect, it } from "vitest";

import { selectTerminalSession, useTerminalWorkbenchStore } from "@/lib/terminal/terminal-workbench-store";
import { endedTerminalSessions, mountedTerminalSessionIds, terminalBadge, terminalTabs } from "@/lib/terminal/terminal-sessions";
import { terminalSession } from "./transport-fixtures";

const SESSION = 42;
const session = () => selectTerminalSession(SESSION)(useTerminalWorkbenchStore.getState());

describe("终端工作台 store", () => {
  beforeEach(() => useTerminalWorkbenchStore.setState({ bySession: {} }));

  it("同一会话投影再次同步不分配状态或通知订阅者", () => {
    const sessions = [terminalSession("t1")];
    const store = useTerminalWorkbenchStore.getState();
    store.syncSessions(SESSION, sessions);
    const before = useTerminalWorkbenchStore.getState();
    let notifications = 0;
    const unsubscribe = useTerminalWorkbenchStore.subscribe(() => { notifications += 1; });
    try {
      store.syncSessions(SESSION, sessions);
      expect(useTerminalWorkbenchStore.getState()).toBe(before);
      expect(notifications).toBe(0);
      store.syncSessions(SESSION, [terminalSession("t1", { status: "exited", exitCode: 2 })]);
      expect(notifications).toBe(1);
      expect(session().activeSessionId).toBe("t1");
      expect(session().sessions?.[0].status).toBe("exited");
    } finally {
      unsubscribe();
    }
  });

  it("按 Session 独立记住面板开关与选中会话", () => {
    const store = useTerminalWorkbenchStore.getState();
    store.syncSessions(SESSION, [terminalSession("t1"), terminalSession("t2")]);
    store.select(SESSION, "t2");
    store.setOpen(SESSION, true);
    store.syncSessions(7, [terminalSession("x1")]);

    expect(session()).toMatchObject({ open: true, activeSessionId: "t2" });
    expect(selectTerminalSession(7)(useTerminalWorkbenchStore.getState())).toMatchObject({ open: false, activeSessionId: "x1" });
    expect(terminalBadge(session().sessions, session().acknowledgedEnds).count).toBe(2);
  });

  it("页头角标：未看过的异常结束提醒，打开面板即视为看过", () => {
    const store = useTerminalWorkbenchStore.getState();
    const badge = () => terminalBadge(session().sessions, session().acknowledgedEnds);
    store.syncSessions(SESSION, [terminalSession("t1"), terminalSession("t2")]);
    expect(badge()).toMatchObject({ count: 2, tone: null, hint: null });

    // 收起时异常退出：标红；正常退出（0）与主动关闭不提醒。
    store.syncSessions(SESSION, [
      terminalSession("t1"),
      terminalSession("t2", { status: "exited", exitCode: 2 }),
      terminalSession("t3", { status: "closed" }),
      terminalSession("t4", { status: "exited", exitCode: 0 }),
    ]);
    expect(badge()).toMatchObject({ count: 1, tone: "attention" });
    // 已结束会话不在标签栏里，仍按编号称呼，与用户看到过的标签名一致。
    expect(badge().hint).toContain("「终端 2」异常结束（退出码 2）");

    store.setOpen(SESSION, true);
    expect(badge().tone).toBeNull();
    // 面板开着时结束的会话用户看得见，收起后也不再提醒。
    store.syncSessions(SESSION, [terminalSession("t1", { status: "failed" }), terminalSession("t2", { status: "exited", exitCode: 2 })]);
    store.setOpen(SESSION, false);
    expect(badge()).toEqual({ count: 0, tone: null, hint: null });
  });

  it("Session 删除后 forget 丢弃其桶，其他 Session 不受影响", () => {
    const store = useTerminalWorkbenchStore.getState();
    store.syncSessions(SESSION, [terminalSession("t1")]);
    store.syncSessions(7, [terminalSession("x1")]);
    const before = useTerminalWorkbenchStore.getState();
    store.forget([99]);
    expect(useTerminalWorkbenchStore.getState()).toBe(before);
    store.forget([SESSION]);
    expect(SESSION in useTerminalWorkbenchStore.getState().bySession).toBe(false);
    expect(useTerminalWorkbenchStore.getState().bySession[7]?.activeSessionId).toBe("x1");
  });

  it("离开 Session 页清空会话镜像但保留面板偏好；已 forget 的 Session 不被重建", () => {
    const store = useTerminalWorkbenchStore.getState();
    store.syncSessions(SESSION, [terminalSession("t1"), terminalSession("t2")]);
    store.select(SESSION, "t2");
    store.setOpen(SESSION, true);
    store.syncSessions(SESSION, null);
    expect(session()).toMatchObject({ sessions: null, open: true, activeSessionId: "t2" });
    expect(terminalBadge(session().sessions, session().acknowledgedEnds).count).toBe(0);

    store.forget([SESSION]);
    store.syncSessions(SESSION, null);
    expect(SESSION in useTerminalWorkbenchStore.getState().bySession).toBe(false);
  });

  it("没有活跃会话时打开面板才标记自动新建；会话列表未知时也标记，等列表到达再判定", () => {
    const store = useTerminalWorkbenchStore.getState();
    store.toggle(SESSION);
    expect(session()).toMatchObject({ open: true, autoCreate: true, sessions: null });
    store.consumeAutoCreate(SESSION);
    store.toggle(SESSION);
    store.syncSessions(SESSION, [terminalSession("t1")]);
    store.toggle(SESSION);
    expect(session()).toMatchObject({ open: true, autoCreate: false });
  });

  it("选中的会话从 snapshot 消失后退到第一个活跃会话；已结束但仍在列表里的保留选中", () => {
    const store = useTerminalWorkbenchStore.getState();
    store.syncSessions(SESSION, [terminalSession("t1"), terminalSession("t2")]);
    store.select(SESSION, "t2");
    store.syncSessions(SESSION, [terminalSession("t1"), terminalSession("t2", { status: "exited", exitCode: 0 })]);
    expect(session().activeSessionId).toBe("t2");
    store.syncSessions(SESSION, [terminalSession("t1")]);
    expect(session().activeSessionId).toBe("t1");
    store.syncSessions(SESSION, []);
    expect(session().activeSessionId).toBeNull();
  });

  it("reveal 打开面板、选中会话并请求焦点", () => {
    const store = useTerminalWorkbenchStore.getState();
    store.syncSessions(SESSION, [terminalSession("t1"), terminalSession("t2")]);
    store.reveal(SESSION, "t2");
    expect(session()).toMatchObject({ open: true, activeSessionId: "t2", autoCreate: false, focusToken: 1 });
  });
});

describe("终端标签命名", () => {
  it("单会话显示「终端」不编号；多会话按序编号", () => {
    expect(terminalTabs([terminalSession("t1")], "t1")).toEqual([
      expect.objectContaining({ title: "终端" }),
    ]);
    const tabs = terminalTabs([
      terminalSession("t1"),
      terminalSession("t2"),
      terminalSession("t3"),
    ], "t1");
    expect(tabs.map((tab) => tab.title)).toEqual(["终端 1", "终端 2", "终端 3"]);
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
    expect(terminalTabs(sessions, "t1").map((tab) => tab.session.terminalSessionId)).toEqual(["t1"]);
    expect(terminalTabs(sessions, "old").map((tab) => tab.session.terminalSessionId)).toEqual(["t1", "old"]);
    expect(endedTerminalSessions(sessions).map((session) => session.terminalSessionId)).toEqual(["old"]);
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
