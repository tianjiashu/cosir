/**
 * PanelDragHandle 组件测试。
 *
 * 验证「明显可抓手柄」这一核心需求：
 * - 渲染为可访问的分隔条（role=separator）并带区分性 aria-label
 * - 命中区宽度大于视觉线宽（w-1.5 命中区 vs 细分隔线视觉）
 * - 依赖库输出的 data-separator 状态属性做样式态（hover/drag/active/focus）
 *
 * 使用 react-dom/server 的 renderToString 在 node 环境下隔离渲染，
 * 不依赖 happy-dom，符合项目既有测试约定（见 VirtualList.test.tsx）。
 *
 * @module tests/PanelDragHandle
 */

import { describe, expect, it } from "vitest";
import { renderToString } from "react-dom/server";
import { createElement } from "react";
import { Group, Panel } from "react-resizable-panels";
import { PanelDragHandle } from "@/components/layout/PanelDragHandle";

// Separator 必须在 Group 上下文内渲染，单独渲染会抛 "Group Context not found"。
function renderHandle(ariaLabel: string, className?: string) {
  return renderToString(
    createElement(
      Group,
      { id: "test-group", orientation: "horizontal" },
      createElement(Panel, { id: "a", defaultSize: "100px", minSize: "50px" }),
      createElement(PanelDragHandle, { ariaLabel, className }),
      createElement(Panel, { id: "b", defaultSize: "100px", minSize: "50px" }),
    ),
  );
}

describe("PanelDragHandle", () => {
  it("渲染为 role=separator 的可拖拽分隔条", () => {
    const html = renderHandle("调整左侧导航栏宽度");
    expect(html).toContain('role="separator"');
  });

  it("透传 aria-label，使屏幕阅读器能区分不同分隔条", () => {
    const left = renderHandle("调整左侧导航栏宽度");
    const right = renderHandle("调整右侧信息面板宽度");
    expect(left).toContain('aria-label="调整左侧导航栏宽度"');
    expect(right).toContain('aria-label="调整右侧信息面板宽度"');
  });

  it("命中区宽度大于视觉线宽，保证易抓取（明显手柄）", () => {
    const html = renderHandle("调整左侧导航栏宽度");
    // 命中区 w-1.5(6px) 远宽于常态细分隔线的视觉宽度，降低误操作。
    expect(html).toContain("w-1.5");
    // 抓握点指示元素存在（带圆角边框的小方块），强化「可抓」视觉。
    expect(html).toContain("rounded-sm");
    expect(html).toContain("border-border");
    // 内部渲染了 svg 图标（lucide GripVertical）。
    expect(html).toContain("<svg");
  });

  it("样式态依赖 data-separator 状态属性（hover/drag/active/focus）", () => {
    const html = renderHandle("调整左侧导航栏宽度");
    // 钩子样式：悬停/聚焦/拖拽/激活四类状态，均基于 data-separator=xxx。
    expect(html).toContain("data-[separator=hover]");
    expect(html).toContain("data-[separator=focus]");
    expect(html).toContain("data-[separator=drag]");
    expect(html).toContain("data-[separator=active]");
  });

  it("支持通过 className 覆盖样式", () => {
    const html = renderHandle("调整左侧导航栏宽度", "custom-handle");
    expect(html).toContain("custom-handle");
  });
});
