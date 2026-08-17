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

/**
 * M3 回归：Sources tab 必须可达且可停留。
 *
 * 历史 bug：activeTab 原为派生值（selectedChildTurnId ? "subagent" : "outputs"），
 * 未选中时永远返回 "outputs"，用户点击 Sources 后 handleTabChange 写入 "sources"，
 * 但下一帧派生值仍强制覆盖回 "outputs"，导致 Sources tab 点击后瞬间跳回 outputs、
 * Sources 永远不可达。修复后 activeTab 为 useState，点击后停留。
 *
 * 注：happy-dom 下 Radix Tabs 的 onValueChange 由 mousedown 链路触发（经诊断，
 * fireEvent.click / fireEvent.pointerDown 均不触发，fireEvent.mouseDown 可触发并
 * 使 data-state 变为 active），故本组测试用 fireEvent.mouseDown 模拟真实指针交互。
 */
describe("RightPanel Sources tab 可达性 (M3)", () => {
  beforeEach(() => {
    useDelegationStore.getState().clearSelection();
  });

  it("未选中时 Sources tab 触发器存在（证明 tab 可达入口未被移除）", () => {
    render(<RightPanel />);

    const sourcesTrigger = screen.getByRole("tab", { name: /sources/i });
    expect(sourcesTrigger).toBeTruthy();
    // 默认激活 outputs，Sources 为 inactive（Radix 未激活 TabsContent 不挂载）。
    expect(sourcesTrigger.getAttribute("data-state")).toBe("inactive");
  });

  it("点击 Sources TabsTrigger 后停留为 sources，且 Sources 专属内容渲染（不再跳回 outputs）", () => {
    render(<RightPanel />);

    const sourcesTrigger = screen.getByRole("tab", { name: /sources/i });
    fireEvent.mouseDown(sourcesTrigger);

    // 停留：tab 仍选中 sources，且未被派生逻辑拉回 outputs。
    expect(sourcesTrigger.getAttribute("data-state")).toBe("active");

    // 激活后 Sources 专属内容（MOCK_SOURCES 的 docs/desktop-client-development-plan.md）
    // 出现在 DOM 中，证明 Sources tab 真实可达并停留。修复前点击 sources 会被派生
    // 逻辑拉回 outputs，Sources 内容永不出现。
    expect(
      screen.getAllByText("docs/desktop-client-development-plan.md").length,
    ).toBeGreaterThan(0);

    // 选中态应被清空（手动切到非 subagent tab 触发 clearSelection）。
    expect(useDelegationStore.getState().selectedChildTurnId).toBeNull();
  });

  it("点击 Outputs TabsTrigger 后选中态被清空（手动切 tab 解耦选中态）", () => {
    // 先选中 child turn（模拟父 timeline 点击 delegation 行）。
    act(() => {
      useDelegationStore.getState().selectChildTurn("turn_sel_clear");
    });
    expect(useDelegationStore.getState().selectedChildTurnId).toBe("turn_sel_clear");

    render(<RightPanel />);

    const outputsTrigger = screen.getByRole("tab", { name: /outputs/i });
    fireEvent.mouseDown(outputsTrigger);

    // 手动切到 outputs（非 subagent）→ 选中态应被清空。
    expect(useDelegationStore.getState().selectedChildTurnId).toBeNull();
    expect(outputsTrigger.getAttribute("data-state")).toBe("active");
  });

  it("选中 child turn 后自动切到 subagent（useEffect 派生同步）", () => {
    render(<RightPanel />);

    act(() => {
      useDelegationStore.getState().selectChildTurn("turn_auto_sub");
    });

    // 自动切到 subagent：tab 激活态为 subagent。
    const subagentTrigger = screen.getByRole("tab", { name: /subagent/i });
    expect(subagentTrigger.getAttribute("data-state")).toBe("active");
    expect(useDelegationStore.getState().selectedChildTurnId).toBe("turn_auto_sub");
  });
});
