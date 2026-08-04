// @vitest-environment happy-dom
/**
 * App 布局持久化测试（happy-dom 环境）。
 *
 * 验证需求「拖拽后持久化」的真实行为——而非无鉴别力的 not.toThrow：
 * useDefaultLayout 按库真实键格式 `react-resizable-panels:{id}:{panelIds.join(":")}`
 * 从 localStorage 读取预置布局并恢复。本测试直接渲染调用该 hook 的组件，
 * 断言其返回的 defaultLayout 确实等于预置值，从而证明「读取恢复」生效。
 *
 * 键格式与值形态均依据 node_modules 内实现核实：
 * - 键：react-resizable-panels/dist/react-resizable-panels.js:1799 `he()`
 * - 值：panelIds 为键、layout 数组为值的对象（非嵌套 {layout:[...]}）
 *
 * @module tests/AppLayout.persistence
 */

import { describe, expect, it, beforeEach } from "vitest";
import { renderToString } from "react-dom/server";
import { createElement } from "react";
import { useDefaultLayout } from "react-resizable-panels";

// 与 App.tsx 保持一致的 id，确保键格式可对照。
const CHAT_LAYOUT_ID = "workbench-layout-chat-v1";
const FULL_LAYOUT_ID = "workbench-layout-full-v1";
const SIDEBAR_PANEL_ID = "sidebar";
const CENTER_PANEL_ID = "center";
const RIGHT_PANEL_ID = "right";

// 库真实键格式：react-resizable-panels:{id}:{panelIds.join(":")}
function chatStorageKey() {
  return `react-resizable-panels:${CHAT_LAYOUT_ID}:${[SIDEBAR_PANEL_ID, CENTER_PANEL_ID, RIGHT_PANEL_ID].join(":")}`;
}
// 渲染读取到的 defaultLayout 到文本，便于断言。若未恢复则为 null。
function LayoutProbe({
  layoutId,
  panelIds,
}: {
  layoutId: string;
  panelIds: string[];
}) {
  const { defaultLayout } = useDefaultLayout({
    id: layoutId,
    panelIds,
    onlySaveAfterUserInteractions: true,
  });
  return createElement(
    "div",
    { "data-testid": "layout" },
    defaultLayout ? JSON.stringify(defaultLayout) : "null",
  );
}

describe("App 布局持久化", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("按库真实键格式读取预置布局并恢复（chat 三栏）", () => {
    // 预置：三栏 layout 对象，键为 panelIds、值为像素宽度数组。
    localStorage.setItem(
      chatStorageKey(),
      JSON.stringify({ [SIDEBAR_PANEL_ID]: 240, [CENTER_PANEL_ID]: 0, [RIGHT_PANEL_ID]: 288 }),
    );

    const html = renderToString(
      createElement(LayoutProbe, {
        layoutId: CHAT_LAYOUT_ID,
        panelIds: [SIDEBAR_PANEL_ID, CENTER_PANEL_ID, RIGHT_PANEL_ID],
      }),
    );

    // 断言确实读取到预置值（而非静默回退 null），证明持久化恢复生效。
    // renderToString 将双引号转义为 &quot;，故用转义形态断言。
    expect(html).toContain("&quot;sidebar&quot;:240");
    expect(html).toContain("&quot;right&quot;:288");
  });

  it("无预置布局时返回 null（默认渲染，不报错）", () => {
    const html = renderToString(
      createElement(LayoutProbe, {
        layoutId: CHAT_LAYOUT_ID,
        panelIds: [SIDEBAR_PANEL_ID, CENTER_PANEL_ID, RIGHT_PANEL_ID],
      }),
    );
    expect(html).toContain(">null<");
  });

  it("两视图持久化键相互独立，互不影响", () => {
    // 仅预置 chat 视图，full 视图应读不到（返回 null）。
    localStorage.setItem(
      chatStorageKey(),
      JSON.stringify({ [SIDEBAR_PANEL_ID]: 240, [CENTER_PANEL_ID]: 0, [RIGHT_PANEL_ID]: 288 }),
    );

    const fullHtml = renderToString(
      createElement(LayoutProbe, {
        layoutId: FULL_LAYOUT_ID,
        panelIds: [SIDEBAR_PANEL_ID, CENTER_PANEL_ID],
      }),
    );
    // full 视图未预置 → 无恢复值，证明 chat 键不会串扰到 full 键。
    expect(fullHtml).toContain(">null<");
  });

  it("键格式误用（缺 panelIds 段）时无法恢复，印证键格式契约", () => {
    // 故意用错误键（仅 id，无 panelIds 段）预置，库应读不到。
    localStorage.setItem(
      `react-resizable-panels:${CHAT_LAYOUT_ID}`,
      JSON.stringify({ [SIDEBAR_PANEL_ID]: 240, [CENTER_PANEL_ID]: 0, [RIGHT_PANEL_ID]: 288 }),
    );
    const html = renderToString(
      createElement(LayoutProbe, {
        layoutId: CHAT_LAYOUT_ID,
        panelIds: [SIDEBAR_PANEL_ID, CENTER_PANEL_ID, RIGHT_PANEL_ID],
      }),
    );
    expect(html).toContain(">null<");
  });
});
