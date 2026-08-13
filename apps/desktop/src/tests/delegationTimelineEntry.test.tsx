// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DelegationTimelineEntry } from "@/components/chat/DelegationTimelineEntry";
import { useDelegationStore } from "@/stores/delegationStore";

const LONG_ID =
  "delegate_reviewer_with_a_very_long_identifier_that_should_not_force_horizontal_overflow";
const LONG_SUMMARY =
  "The delegated child agent produced a very long summary with/unbroken/path/segments/and_additional_context_that_must_wrap_inside_the_timeline_entry.";

describe("DelegationTimelineEntry", () => {
  it("renders status, child agent id, child turn id, and summary", () => {
    render(
      <DelegationTimelineEntry
        childAgentId="delegate_reviewer"
        childTurnId="turn_child"
        delegationType="review"
        status="completed"
        summary="review passed"
        childEntries={[]}
      />,
    );

    expect(screen.getByText("completed")).toBeTruthy();
    expect(screen.getByText("delegate_reviewer")).toBeTruthy();
    expect(screen.getByText("turn_child")).toBeTruthy();
    expect(screen.getByText("review passed")).toBeTruthy();
  });

  it("uses shrinkable and wrapping classes for long ids and summaries", () => {
    const { container } = render(
      <DelegationTimelineEntry
        childAgentId={LONG_ID}
        childTurnId="turn_child_with_a_very_long_identifier_that_should_wrap_safely"
        delegationType="review"
        status="failed"
        error={LONG_SUMMARY}
        childEntries={[]}
      />,
    );

    expect(screen.getByText(LONG_ID).className).toMatch(/break-all/);
    expect(screen.getByText(LONG_SUMMARY).className).toMatch(/break-words|break-all/);
    expect(container.firstElementChild?.className).toContain("min-w-0");
    expect(container.innerHTML).not.toMatch(/w-\[[^\]]+\]|max-w-\[[^\]]+\]/);
  });

  it("expands delegated child entries when provided", () => {
    render(
      <DelegationTimelineEntry
        childAgentId="delegate_reviewer"
        childTurnId="turn_child"
        delegationType="review"
        status="running"
        childEntries={<div>child timeline output</div>}
      />,
    );

    expect(screen.queryByText("child timeline output")).toBeNull();

    fireEvent.click(screen.getByLabelText("Expand delegated child events"));

    expect(screen.getByText("child timeline output")).toBeTruthy();
  });

  it("焦点在展开箭头按钮上按 Enter 不打开侧边栏（兄弟原生 button，无外层 role=button 冒泡键盘 bug）", () => {
    render(
      <DelegationTimelineEntry
        childAgentId="delegate_reviewer"
        childTurnId="turn_child_kbd_arrow"
        delegationType="review"
        status="running"
        childEntries={<div>child timeline output</div>}
      />,
    );

    // 修复前：外层 role=button 的 onKeyDown 会吞掉箭头 Enter 并误触发 openSidePanel。
    // 修复后：展开箭头与「打开侧边栏」是同级原生 button，焦点在箭头上按 Enter 不应触达侧边栏选中。
    const arrow = screen.getByLabelText("Expand delegated child events");
    fireEvent.keyDown(arrow, { key: "Enter" });

    // 关键回归点：选中态未被误写，侧边栏入口按钮仍未被激活。
    expect(useDelegationStore.getState().selectedChildTurnId).toBeNull();
    expect(screen.getByLabelText("Open delegate_reviewer child timeline in side panel")).toBeTruthy();
  });
});
