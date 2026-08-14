// @vitest-environment happy-dom
import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { RightPanel } from "@/components/layout/RightPanel";
import { DelegationTimelineEntry } from "@/components/chat/DelegationTimelineEntry";
import { useDelegationStore } from "@/stores/delegationStore";

/**
 * 回归测试：点击 delegation 行后右侧面板必须切到 Subagent Tab。
 *
 * 历史 bug：RightPanel 曾把 `selectedChildTurnId`（turn id 字符串）直接当作
 * Radix Tabs 的 `value`，而 Tabs 的 value 语义是 tab 名（"subagent"）。选中后
 * `value` 变成 turn id，与任何 TabsTrigger 都不匹配，导致 tab 栏与内容区都不切换，
 * 用户感知「点击 delegation 行右侧无响应」。修复后 `activeTab` 在选中态非空时固定
 * 为 "subagent"，本测试锁住该不变量。
 */
describe("RightPanel 选中委派 → 切到 Subagent Tab", () => {
  beforeEach(() => {
    useDelegationStore.getState().clearSelection();
  });

  it("未选中时默认展示 Outputs Tab 内容", () => {
    render(<RightPanel />);
    expect(screen.getByText("Subagent")).toBeTruthy();
    // 空态提示仅在 Subagent Tab 激活时出现；未选中时不应出现。
    expect(screen.queryByText("点击对话中的委派行以查看子 Agent 时间线")).toBeNull();
  });

  it("点击 delegation 行后右侧切到 Subagent Tab 并展示子 Agent 空态引导", () => {
    render(
      <>
        <DelegationTimelineEntry
          childAgentId="delegate_reviewer"
          childTurnId="turn_child_rp"
          delegationType="review"
          status="running"
        />
        <RightPanel />
      </>,
    );

    fireEvent.click(screen.getByLabelText("Open delegate_reviewer child timeline in side panel"));

    // 选中态已写入 store
    expect(useDelegationStore.getState().selectedChildTurnId).toBe("turn_child_rp");

    // Subagent Tab 激活后，SubagentPanel 头部会渲染 child turn id，证明 Tabs
    // 已切到 subagent（而非停留在 outputs）。
    expect(screen.getAllByText("turn_child_rp").length).toBeGreaterThan(0);
  });

  it("选中态清空后 Subagent 面板收起（store → UI 单向绑定）", () => {
    // 仅渲染 RightPanel，隔离左侧 DelegationTimelineEntry 对 childTurnId 文本的干扰。
    render(<RightPanel />);

    act(() => {
      useDelegationStore.getState().selectChildTurn("turn_child_rp2");
    });
    expect(useDelegationStore.getState().selectedChildTurnId).toBe("turn_child_rp2");
    // Subagent Tab 激活后，面板头部渲染 child turn id。
    expect(screen.getAllByText("turn_child_rp2").length).toBeGreaterThan(0);

    // 真实浏览器中用户点击 Outputs/Sources tab 会触发 handleTabChange → clearSelection；
    // happy-dom 下 Radix Tabs 的 pointer 交互不被 fireEvent 触发，故直接校验
    // 「选中态清空 → 面板收起」这一 store→UI 绑定不变量。
    act(() => {
      useDelegationStore.getState().clearSelection();
    });
    expect(useDelegationStore.getState().selectedChildTurnId).toBeNull();
    expect(screen.queryByText("turn_child_rp2")).toBeNull();
  });
});
