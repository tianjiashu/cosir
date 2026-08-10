// @vitest-environment happy-dom
/**
 * 缺陷验证 #9：ToolCallCard 失败态折叠行摘要无红色高亮（三元两分支相同的死代码）。
 *
 * 背景：组件文件头注释承诺「失败时红色高亮错误主因」（约第 5 行），但约 294 行：
 *   status === "error" ? "text-muted-foreground" : "text-muted-foreground"
 * 两分支完全相同，失败态折叠行摘要与成功/运行态同为灰色，用户无法在一屏内
 * 一眼定位失败工具。正确行为：error 态摘要应使用 destructive/red 系高亮。
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ToolCallCard } from "@/components/chat/ToolCallCard";

// 打开文件动作经 Tauri IPC，本测试不触达，mock 防御导入副作用。
vi.mock("@/services/backend", () => ({
  openFileInEditor: vi.fn().mockResolvedValue(undefined),
}));

describe("ToolCallCard 失败态折叠行高亮", () => {
  // 测试目的：status="error" 的折叠行摘要文本应带 destructive/red 高亮 class。
  // 可能发现的缺陷：三元表达式两分支相同的死代码，失败态与常规态同为
  //   text-muted-foreground，违背组件声明的「失败红色高亮」契约。
  it("status=error 时折叠行摘要应以红色/destructive 高亮", () => {
    render(<ToolCallCard toolName="read_file" status="error" error="权限不足" />);

    // 折叠态摘要 = "read_file 权限不足"（无 display 降级路径）。
    const summary = screen.getByText(/权限不足/);
    expect(summary.className).toMatch(/destructive|red/);
  });

  // 测试目的：正向对照——completed 态折叠行摘要本就不应高亮（灰色为正确样式），
  //   证明断言针对的是 error 专属样式而非误报。
  // 可能发现的缺陷：无（此用例应 PASS）。
  it("对照：status=completed 时折叠行摘要保持常规灰色（非高亮）", () => {
    render(
      <ToolCallCard
        toolName="read_file"
        status="completed"
        resultSummary="读取完成"
        result="file content"
      />,
    );
    const summary = screen.getByText(/读取完成/);
    expect(summary.className).not.toMatch(/destructive/);
    expect(summary.className).toContain("text-muted-foreground");
  });
});
