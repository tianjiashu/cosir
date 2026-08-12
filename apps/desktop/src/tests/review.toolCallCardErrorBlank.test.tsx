// @vitest-environment happy-dom
/**
 * 回归测试：ToolCallCard 失败态不再整体空白。
 *
 * 修复前：折叠行展开 chevron 条件 `status !== "error"`，展开体条件 `status === "completed"`；
 * 失败态工具调用既无 chevron 也无展开详情，失败上下文（目标路径、参数、错误）不可见。
 *
 * 修复后：失败态可展开，展开后展示错误主因/原因/可重试徽标与参数区；
 * 紧凑型（isChangeLayout）视图也补充了 error 态展开体。
 *
 * @module tests/review.toolCallCardErrorBlank
 */

import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

vi.mock("@/services/backend", () => ({
  openFileInEditor: vi.fn().mockResolvedValue(undefined),
}));

import { ToolCallCard } from "@/components/chat/ToolCallCard";

describe("ToolCallCard 失败态不再空白（回归）", () => {
  it("error 态折叠行出现可展开 chevron（修复后）", () => {
    const { container } = render(
      <ToolCallCard
        toolName="write_file"
        status="error"
        error="路径超出 workspace 边界"
        args={{ path: "/etc/passwd", content: "x" }}
      />,
    );
    const chevrons = container.querySelectorAll("svg.lucide-chevron-right, svg[class*='chevron-right']");
    expect(chevrons.length).toBeGreaterThan(0);
  });

  it("error 态点击展开后可见错误专属区块与参数（修复后）", () => {
    render(
      <ToolCallCard
        toolName="write_file"
        status="error"
        error="路径超出 workspace 边界"
        args={{ path: "/etc/passwd", content: "x" }}
      />,
    );
    // 点击折叠触发区展开
    const trigger = screen.getByRole("button");
    fireEvent.click(trigger);
    // 展开后 error 专属区块必须渲染（证明缺陷2 修复点：error 态展开体不再空白）
    expect(screen.getByText("错误:")).toBeDefined();
    expect(screen.getByText("重试:")).toBeDefined();
    // 失败主因与参数区均可见
    expect(screen.getByText("路径超出 workspace 边界")).toBeDefined();
    expect(screen.getByText(/\/etc\/passwd/)).toBeDefined();
    expect(screen.getByText("参数:")).toBeDefined();
  });

  it("对照：completed 态依旧可展开且展示参数（基线行为不回退）", () => {
    render(
      <ToolCallCard
        toolName="write_file"
        status="completed"
        resultSummary="已写入 /etc/passwd"
        result="written"
        args={{ path: "/etc/passwd", content: "x" }}
      />,
    );
    const trigger = screen.getByRole("button");
    fireEvent.click(trigger);
    expect(screen.getAllByText(/etc\/passwd/).length).toBeGreaterThan(0);
  });
});
