import { afterEach, describe, expect, it } from "vitest";

import type { HandoffEntry } from "@/lib/assistant/contract";
import { useBrowserPanelStore } from "@/lib/browser/panel-store";
import { useHandoffStore } from "@/lib/handoff/handoff-store";
import { useDockStore } from "@/lib/layout/dock-store";

const SESSION = 7;
const browser: HandoffEntry = { toolCallId: "c2", toolName: "browser_handoff", resourceId: "t3", reason: "登录" };

afterEach(() => {
  useHandoffStore.setState({ sessions: {} });
  useDockStore.setState({ panel: null, maximized: false });
  useBrowserPanelStore.setState({ sessionId: null, selected: {} });
});

describe("handoff store", () => {
  it("新出现的浏览器交接打开浏览器面板并选中那个标签", () => {
    useHandoffStore.getState().sync(SESSION, []);
    useHandoffStore.getState().sync(SESSION, [browser]);
    expect(useDockStore.getState().panel).toBe("browser");
    expect(useBrowserPanelStore.getState()).toMatchObject({ sessionId: SESSION, selected: { [SESSION]: "t3" } });
  });

  it("离开 Session 后清空镜像，回来时已在进行的交接再弹出一次；删除 Session 丢弃桶", () => {
    useHandoffStore.getState().sync(SESSION, [browser]);
    useDockStore.getState().close();
    useHandoffStore.getState().sync(SESSION, null);
    useHandoffStore.getState().sync(SESSION, [browser]);
    expect(useDockStore.getState().panel).toBe("browser");

    useHandoffStore.getState().forget([SESSION]);
    expect(SESSION in useHandoffStore.getState().sessions).toBe(false);
  });
});
