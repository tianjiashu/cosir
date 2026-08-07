// @vitest-environment happy-dom
/**
 * 模型思考中指示器与 TurnTimeline「等待首 token」状态的回归测试。
 *
 * 守护不变量：
 * 1. ThinkingIndicator 渲染「思考中」文本与三个跳动圆点；
 * 2. TurnTimeline 在「用户已输入 + 无条目 + 无最终回复」的空窗期显示指示器，
 *    一旦 response_text 到达或首条事件到达即停止显示，避免与正式输出重复。
 */
import { describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";
import type { TurnRecord } from "@shared/turn";
import { ThinkingIndicator } from "@/components/chat/ThinkingIndicator";
import { TurnTimeline } from "@/components/layout/TurnTimeline";

// openFileInEditor 来自 Tauri 宿主模块，测试环境无 Tauri，mock 掉以免导入崩溃。
vi.mock("@/services/backend", () => ({
  openFileInEditor: vi.fn().mockResolvedValue(undefined),
}));
// 其余 chat 子组件不参与本测试断言，mock 为占位即可。
vi.mock("@/components/chat/UserMessage", () => ({ UserMessage: () => null }));
vi.mock("@/components/chat/AgentMessage", () => ({ AgentMessage: () => null }));
vi.mock("@/components/chat/ThinkingBlock", () => ({ ThinkingBlock: () => null }));
vi.mock("@/components/chat/ToolCallCard", () => ({ ToolCallCard: () => null }));
vi.mock("@/components/chat/TerminalCallCard", () => ({ TerminalCallCard: () => null }));
vi.mock("@/components/chat/StatusBadge", () => ({ StatusBadge: () => null }));

function makeTurn(overrides: Partial<TurnRecord> = {}): TurnRecord {
  return {
    turn_id: "turn-1",
    task_id: "task-1",
    input_text: "请帮我修复这个 bug",
    status: "pending",
    end_reason: null,
    response_text: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    ...overrides,
  } as unknown as TurnRecord;
}

describe("ThinkingIndicator", () => {
  it("渲染『思考中』文案与三个跳动圆点", () => {
    const { getByTestId, getByText } = render(<ThinkingIndicator />);
    expect(getByText("思考中")).toBeTruthy();
    const dots = getByTestId("thinking-dots");
    expect(dots.querySelectorAll("span.thinking-dot")).toHaveLength(3);
  });
});

describe("TurnTimeline 等待首 token 状态", () => {
  it("用户已输入但无事件、无回复时显示思考中指示器", () => {
    const turn = makeTurn();
    const { getByTestId } = render(<TurnTimeline turn={turn} events={[]} />);
    expect(getByTestId("thinking-indicator")).toBeTruthy();
  });

  it("首条 runtime 事件到达后不再显示指示器（避免与正式输出重复）", () => {
    const turn = makeTurn();
    const event = {
      event_id: "ev-1",
      task_id: "task-1",
      turn_id: "turn-1",
      event_type: "model_output_delta",
      sequence: 1,
      created_at: new Date().toISOString(),
      payload: { text: "正在分析" },
    } as unknown as Parameters<typeof TurnTimeline>[0]["events"][number];
    const { queryByTestId } = render(<TurnTimeline turn={turn} events={[event]} />);
    expect(queryByTestId("thinking-indicator")).toBeNull();
  });

  it("最终回复到达后不再显示指示器", () => {
    const turn = makeTurn({ response_text: "已修复" });
    const { queryByTestId } = render(<TurnTimeline turn={turn} events={[]} />);
    expect(queryByTestId("thinking-indicator")).toBeNull();
  });

  it("纯空白 turn（无输入/无事件/无回复）整体不渲染，指示器也不显示", () => {
    const turn = makeTurn({ input_text: "" });
    const { queryByTestId, container } = render(<TurnTimeline turn={turn} events={[]} />);
    expect(queryByTestId("thinking-indicator")).toBeNull();
    // 空态早返回使整条 timeline 不挂载任何节点。
    expect(container.firstChild).toBeNull();
  });
});
