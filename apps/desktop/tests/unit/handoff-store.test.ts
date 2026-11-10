import { afterEach, describe, expect, it } from "vitest";

import type { HandoffEntry } from "@/lib/assistant/contract";
import { useBrowserPanelStore } from "@/lib/browser/panel-store";
import { useHandoffStore } from "@/lib/handoff/handoff-store";
import { useDockStore } from "@/lib/layout/dock-store";

const TASK = 7;
const browser: HandoffEntry = { toolCallId: "c2", toolName: "browser_handoff", resourceId: "t3", reason: "登录" };

afterEach(() => {
  useHandoffStore.setState({ tasks: {} });
  useDockStore.setState({ panel: null, maximized: false });
  useBrowserPanelStore.setState({ taskId: null, selected: {} });
});

describe("handoff store", () => {
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
