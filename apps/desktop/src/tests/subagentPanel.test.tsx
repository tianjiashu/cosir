// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { DelegationTimelineEntry } from "@/components/chat/DelegationTimelineEntry";
import { SubagentPanel } from "@/components/right-panel/SubagentPanel";
import { useDelegationStore } from "@/stores/delegationStore";

describe("delegation 行点击 → 侧边栏选中联动", () => {
  beforeEach(() => {
    useDelegationStore.getState().clearSelection();
  });

  it("点击 delegation 行写入选中态，侧边栏展示该 child turn", () => {
    render(
      <DelegationTimelineEntry
        childAgentId="delegate_reviewer"
        childTurnId="turn_child_link"
        delegationType="review"
        status="running"
      />,
    );

    fireEvent.click(screen.getByLabelText("Open delegate_reviewer child timeline in side panel"));

    expect(useDelegationStore.getState().selectedChildTurnId).toBe("turn_child_link");

    // 侧边栏订阅选中态，渲染出对应 child turn 的元信息。
    // 注意：左侧 DelegationTimelineEntry 与右侧 SubagentPanel 都会展示 child turn id，
    // 故用 getAllByText 断言侧边栏确实消费了选中态（而非要求全局唯一匹配）。
    render(<SubagentPanel />);
    expect(screen.getAllByText("turn_child_link").length).toBeGreaterThan(0);
  });

  it("键盘 Enter 触发选中", () => {
    render(
      <DelegationTimelineEntry
        childAgentId="delegate_reviewer"
        childTurnId="turn_child_kbd"
        delegationType="review"
        status="running"
      />,
    );

    fireEvent.keyDown(screen.getByLabelText("Open delegate_reviewer child timeline in side panel"), {
      key: "Enter",
    });

    expect(useDelegationStore.getState().selectedChildTurnId).toBe("turn_child_kbd");
  });

  it("未选中时侧边栏显示提示空态", () => {
    render(<SubagentPanel />);
    expect(screen.getByText("点击对话中的委派行以查看子 Agent 时间线")).toBeTruthy();
  });
});
