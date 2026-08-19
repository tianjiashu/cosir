// @vitest-environment happy-dom
/**
 * ThinkingBlock 展示粒度过滤能力测试（方案 §阶段 3.4 展示开关的纯前端 UI 壳）。
 *
 * 守护不变量：
 * 1. 缺省 display 为 `full`：原样展示完整思考内容（保持既有行为，不回归）；
 * 2. `summary`：仅展示内容前缀摘要预览（SUMMARY_PREVIEW_LENGTH=200），
 *    超过预览长度时截断并加省略号，避免完整思考文本长时间占据对话主区；
 * 3. `off`：关闭思考展示，不渲染任何元素（等价于内容为空时的行为）；
 * 4. 摘要模式下长度不超过预览限值的内容不被截断（不产生多余的省略号）。
 *
 * 该能力是纯前端过滤（不改共享协议）；后端 `thinking_display` 字段由主 Agent
 * 协调协议扩展后接入设置面板，此处仅验证展示粒度过滤本身。
 *
 * @module tests/thinkingBlockDisplay
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ThinkingBlock } from "@/components/chat/ThinkingBlock";

vi.mock("@/lib/logger", () => ({
  logInfo: vi.fn(),
  logWarn: vi.fn(),
  logError: vi.fn(),
  logDebug: vi.fn(),
}));

/** 生成超过摘要预览限值（200 字符）的长思考文本，用于验证截断。 */
function makeLongContent(): string {
  return "思".repeat(300);
}

describe("ThinkingBlock 展示粒度 — full（完整）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("缺省（display 未传）时原样展示完整内容", () => {
    render(<ThinkingBlock content="完整思考内容 ABC" />);
    fireEvent.click(screen.getByText("深度思考"));
    // 完整内容应出现在展开区（MarkdownStream 渲染为文本节点）。
    expect(screen.getByText(/完整思考内容 ABC/)).toBeTruthy();
  });

  it("display=full 时不截断长内容", () => {
    const long = makeLongContent();
    render(<ThinkingBlock content={long} display="full" />);
    fireEvent.click(screen.getByText("深度思考"));
    expect(screen.getByText(long)).toBeTruthy();
    expect(screen.queryByText(/…$/)).toBeNull();
  });
});

describe("ThinkingBlock 展示粒度 — summary（摘要预览）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("summary 超过预览长度时截断为前缀 + 省略号", () => {
    const long = makeLongContent();
    render(<ThinkingBlock content={long} display="summary" />);
    fireEvent.click(screen.getByText("深度思考"));
    // 只出现前缀预览（200 字符 + …），完整 300 字符内容不出现。
    expect(screen.getByText(/^思{200}…$/)).toBeTruthy();
    expect(screen.queryByText(long)).toBeNull();
  });

  it("summary 内容不超过预览长度时不截断（不产生省略号）", () => {
    render(<ThinkingBlock content="短思考内容" display="summary" />);
    fireEvent.click(screen.getByText("深度思考"));
    expect(screen.getByText(/短思考内容/)).toBeTruthy();
    expect(screen.queryByText(/…$/)).toBeNull();
  });
});

describe("ThinkingBlock 展示粒度 — off（关闭）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("display=off 时不渲染任何元素", () => {
    const { container } = render(<ThinkingBlock content="有思考内容" display="off" />);
    expect(container.firstChild).toBeNull();
    expect(screen.queryByText("深度思考")).toBeNull();
  });

  it("display=off 时连流式内容也不渲染", () => {
    const { container } = render(
      <ThinkingBlock content="流式思考" display="off" streaming />,
    );
    expect(container.firstChild).toBeNull();
  });
});
