// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DelegationTimelineEntry } from "@/components/chat/DelegationTimelineEntry";
import { useDelegationStore } from "@/stores/delegationStore";

beforeEach(() => {
  useDelegationStore.getState().clearSelection();
});

const LONG_ID =
  "delegate_reviewer_with_a_very_long_identifier_that_should_not_force_horizontal_overflow";
const LONG_SUMMARY =
  "The delegated child agent produced a very long summary with/unbroken/path/segments/and_additional_context_that_must_wrap_inside_the_timeline_entry.";

describe("DelegationTimelineEntry", () => {
  it("renders status, child agent id, child turn id, and a collapsed result trigger", () => {
    render(
      <DelegationTimelineEntry
        childAgentId="delegate_reviewer"
        childTurnId="turn_child"
        delegationType="review"
        status="completed"
        summary="review passed"
      />,
    );

    expect(screen.getByText("completed")).toBeTruthy();
    expect(screen.getByText("delegate_reviewer")).toBeTruthy();
    expect(screen.getByText("turn_child")).toBeTruthy();
    // 默认折叠：markdown 内容不可见，仅渲染折叠触发条文案。
    expect(screen.getByText("查看子 Agent 结果")).toBeTruthy();
    expect(screen.queryByText("review passed")).toBeNull();
  });

  it("uses shrinkable and wrapping classes for long ids and hides collapsed summary", () => {
    const { container } = render(
      <DelegationTimelineEntry
        childAgentId={LONG_ID}
        childTurnId="turn_child_with_a_very_long_identifier_that_should_wrap_safely"
        delegationType="review"
        status="failed"
        error={LONG_SUMMARY}
      />,
    );

    expect(screen.getByText(LONG_ID).className).toMatch(/break-all/);
    // 折叠态下 LONG_SUMMARY 作为 markdown 内容未渲染到可见 DOM。
    expect(screen.queryByText(LONG_SUMMARY)).toBeNull();
    expect(screen.getByText("查看子 Agent 错误详情")).toBeTruthy();
    expect(container.firstElementChild?.className).toContain("min-w-0");
    expect(container.innerHTML).not.toMatch(/w-\[[^\]]+\]|max-w-\[[^\]]+\]/);
  });

  it("默认折叠：展开后 markdown 内容可见且箭头旋转", () => {
    render(
      <DelegationTimelineEntry
        childAgentId="delegate_reviewer"
        childTurnId="turn_child"
        delegationType="review"
        status="completed"
        summary={"# 子 Agent 结论\n\n- 通过\n- 已修复"}
      />,
    );

    const trigger = screen.getByText("查看子 Agent 结果");
    const triggerButton = trigger.closest("button");
    expect(triggerButton?.getAttribute("data-state")).toBe("closed");

    // 折叠态 markdown 不可见
    expect(screen.queryByText("子 Agent 结论")).toBeNull();

    fireEvent.click(trigger);
    // 展开后 markdown 标题渲染可见
    expect(screen.getByText("子 Agent 结论")).toBeTruthy();
    expect(triggerButton?.getAttribute("data-state")).toBe("open");
  });

  it("展开后渲染 markdown 富组件（代码块）", () => {
    render(
      <DelegationTimelineEntry
        childAgentId="delegate_reviewer"
        childTurnId="turn_child"
        delegationType="review"
        status="completed"
        summary={"```ts\nconst x = 1;\n```"}
      />,
    );

    fireEvent.click(screen.getByText("查看子 Agent 结果"));
    // CodeBlock 富展示：代码文本渲染到 <code> 内。
    expect(screen.getByText(/const x = 1;/)).toBeTruthy();
  });

  it("失败态触发条文案为「查看子 Agent 错误详情」", () => {
    render(
      <DelegationTimelineEntry
        childAgentId="delegate_reviewer"
        childTurnId="turn_child"
        delegationType="review"
        status="failed"
        error="boom"
      />,
    );

    expect(screen.getByText("查看子 Agent 错误详情")).toBeTruthy();
    expect(screen.getByLabelText("展开子 Agent 错误详情")).toBeTruthy();
    fireEvent.click(screen.getByText("查看子 Agent 错误详情"));
    expect(screen.getByText("boom")).toBeTruthy();
  });

  it("取消态按失败态处理，渲染错误详情触发条", () => {
    render(
      <DelegationTimelineEntry
        childAgentId="delegate_reviewer"
        childTurnId="turn_child"
        delegationType="review"
        status="cancelled"
        error="user cancelled"
      />,
    );

    expect(screen.getByText("查看子 Agent 错误详情")).toBeTruthy();
    fireEvent.click(screen.getByText("查看子 Agent 错误详情"));
    expect(screen.getByText("user cancelled")).toBeTruthy();
  });

  it("无 summary 且无 error 时不渲染折叠区", () => {
    render(
      <DelegationTimelineEntry
        childAgentId="delegate_reviewer"
        childTurnId="turn_child"
        delegationType="review"
        status="running"
      />,
    );

    expect(screen.queryByText("查看子 Agent 结果")).toBeNull();
    expect(screen.queryByText("查看子 Agent 错误详情")).toBeNull();
  });

  it("整行点击（原生 button）派发选中态到侧边栏", () => {
    render(
      <DelegationTimelineEntry
        childAgentId="delegate_reviewer"
        childTurnId="turn_child_click"
        delegationType="review"
        status="running"
      />,
    );

    // 整行点击目标即「打开侧边栏」原生 button（非折叠箭头，已移除内联展开）。
    fireEvent.click(screen.getByLabelText("Open delegate_reviewer child timeline in side panel"));

    expect(useDelegationStore.getState().selectedChildTurnId).toBe("turn_child_click");
  });

  it("childTurnId 缺失时整行按钮 disabled，点击不派发选中态", () => {
    render(
      <DelegationTimelineEntry
        childAgentId="delegate_reviewer"
        delegationType="review"
        status="running"
      />,
    );

    // 空 childTurnId 时 aria-label 未定义（无侧边栏入口语义），按钮仅作 disabled 展示。
    const button = screen.getByRole("button", { name: /delegate_reviewer/ });
    expect(button.hasAttribute("disabled")).toBe(true);

    fireEvent.click(button);
    expect(useDelegationStore.getState().selectedChildTurnId).toBeNull();
  });

  it("传入并发字段（size=2, index=0）时渲染「并发 1/2」文案", () => {
    render(
      <DelegationTimelineEntry
        childAgentId="delegate_reviewer"
        childTurnId="turn_child"
        delegationType="review"
        status="running"
        concurrencyGroupSize={2}
        concurrencyIndex={0}
      />,
    );

    // index 从 0 起 → 展示 1/2；非并发场景不渲染该文案。
    expect(screen.getByText("并发 1 / 2")).toBeTruthy();
    expect(screen.queryByText("并发 ? / 2")).toBeNull();
  });

  it("非并发（size 缺失或 < 2）时不渲染并发文案", () => {
    const { rerender } = render(
      <DelegationTimelineEntry
        childAgentId="delegate_reviewer"
        childTurnId="turn_child"
        delegationType="review"
        status="running"
      />,
    );
    expect(screen.queryByText(/并发/)).toBeNull();

    rerender(
      <DelegationTimelineEntry
        childAgentId="delegate_reviewer"
        childTurnId="turn_child"
        delegationType="review"
        status="running"
        concurrencyGroupSize={1}
        concurrencyIndex={0}
      />,
    );
    expect(screen.queryByText(/并发/)).toBeNull();
  });
});
