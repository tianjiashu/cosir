import { afterEach, describe, expect, it } from "vitest";

import type { HandoffEntry } from "@/lib/assistant/contract";
import { useBrowserPanelStore } from "@/lib/browser/panel-store";
import { useHandoffStore } from "@/lib/handoff/store";
import { useDockStore } from "@/lib/layout/dock-store";
import { selectTerminalTask, useTerminalWorkbenchStore } from "@/lib/terminal/terminal-workbench-store";

const TASK = 7;
const terminal: HandoffEntry = { toolCallId: "c1", toolName: "terminal_handoff", surface: "terminal", resourceId: "term_1", reason: "输入密码" };
const browser: HandoffEntry = { toolCallId: "c2", toolName: "browser_handoff", surface: "browser", resourceId: "t3", reason: "登录" };

afterEach(() => {
  useHandoffStore.setState({ tasks: {} });
  useTerminalWorkbenchStore.setState({ tasks: {} });
  useDockStore.setState({ panel: null, maximized: false });
  useBrowserPanelStore.setState({ taskId: null, selected: {} });
});

describe("handoff store", () => {
  it("新出现的终端交接打开终端面板并聚焦那个会话；同一交接持续存在不重复弹出", () => {
    useHandoffStore.getState().sync(TASK, [terminal]);
    expect(selectTerminalTask(TASK)(useTerminalWorkbenchStore.getState())).toMatchObject({ open: true, activeSessionId: "term_1" });

    // 用户收起面板后，同一次交接再次推送也不会把面板弹回来。
    useTerminalWorkbenchStore.getState().setOpen(TASK, false);
    useHandoffStore.getState().sync(TASK, [terminal]);
    useHandoffStore.getState().sync(TASK, [{ ...terminal }]);
    expect(selectTerminalTask(TASK)(useTerminalWorkbenchStore.getState()).open).toBe(false);
  });

  it("新出现的浏览器交接打开浏览器面板并选中那个标签", () => {
    useHandoffStore.getState().sync(TASK, []);
    useHandoffStore.getState().sync(TASK, [browser]);
    expect(useDockStore.getState().panel).toBe("browser");
    expect(useBrowserPanelStore.getState()).toMatchObject({ taskId: TASK, selected: { [TASK]: "t3" } });
  });

  it("离开 Task 后清空镜像，回来时已在进行的交接再弹出一次；删除 Task 丢弃桶", () => {
    useHandoffStore.getState().sync(TASK, [browser]);
    useDockStore.getState().close();
    useHandoffStore.getState().sync(TASK, null);
    useHandoffStore.getState().sync(TASK, [browser]);
    expect(useDockStore.getState().panel).toBe("browser");

    useHandoffStore.getState().forget([TASK]);
    expect(TASK in useHandoffStore.getState().tasks).toBe(false);
  });
});
