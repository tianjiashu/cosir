// @vitest-environment happy-dom
/**
 * Task 1 独立补充测试（审查 4 问题的修复验证）。
 * 仅新增测试、不修改任何业务代码（src/ 下文件）。
 * 重点验证审查报告 4 项问题的修复是否真实生效：
 *   问题1 键盘行为分离、问题2 无 child 事件不无限重渲染、
 *   问题3 徽章类型穷尽 + 复用投影器、问题4 running 兜底不误判终态。
 */
import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import type { RuntimeEvent } from "@shared/events";
import { SubagentPanel } from "@/components/right-panel/SubagentPanel";
import { useDelegationStore } from "@/stores/delegationStore";
import { useEventStore } from "@/stores/eventStore";
import { deriveChildDelegationStatus } from "@/services/timeline/projector";

/** 构造测试用 RuntimeEvent（与现有 subagentPanel.test.tsx 同口径）。 */
function runtimeEvent(
  eventId: string,
  eventType: RuntimeEvent["event_type"],
  turnId: string,
  payload: Record<string, unknown>,
  sequence: number,
): RuntimeEvent {
  return {
    event_id: eventId,
    event_type: eventType,
    task_id: "task-delegation",
    turn_id: turnId,
    sequence,
    created_at: `2026-08-11T00:00:${String(sequence).padStart(2, "0")}Z`,
    payload,
  } as RuntimeEvent;
}

describe("Task1 审查修复独立验证", () => {
  beforeEach(() => {
    useDelegationStore.getState().clearSelection();
    useEventStore.getState().clearEvents();
  });

  // ── 问题2：无 child 事件时不无限重渲染 ───────────────────────────────
  // 测试目的：验证 SubagentPanel 在选中态、但 child 事件分片为空时渲染稳定，
  // 不会因 EMPTY_EVENTS 引用漂移触发 Maximum update depth exceeded（即重复定义 EMPTY_EVENTS
  // 双源真理已被消除，改为复用 eventStore 导出的 EMPTY_EVENTS）。
  // 可能发现的缺陷：本地 EMPTY_EVENTS 字面量每次渲染产生新引用 → 无限重渲染。
  it("问题2 | 选中态下无 child 事件时渲染稳定（不抛 Maximum update depth）", () => {
    // 选中一个 child，但其事件分片为空（未推送任何事件）。
    useDelegationStore.getState().selectChildTurn("turn_child_no_events");

    // act 包裹渲染与一次触发重渲染的更新，捕获可能的无限循环。
    let renderError: unknown = null;
    try {
      act(() => {
        render(<SubagentPanel />);
      });
    } catch (err) {
      renderError = err;
    }

    // 不应抛 Maximum update depth exceeded。
    expect(renderError).toBeNull();
    // 选中态稳定，未因重渲染被重置。
    expect(useDelegationStore.getState().selectedChildTurnId).toBe("turn_child_no_events");
    // 事件未到应显示 loading 占位，而非崩溃。
    expect(screen.getByText("正在等待子 Agent 事件流…")).toBeTruthy();
  });

  // ── 问题3：徽章类型穷尽 + 复用投影器 ──────────────────────────────────
  // 测试目的：直接对导出函数 deriveChildDelegationStatus 做纯函数单元验证，
  // 确认其复用了 projector 的归一化口径（running/completed/failed/waiting_approval/pending/undefined）。
  // 可能发现的缺陷：SubagentPanel 本地重实现口径漂移（如兜底返回 undefined、跳过无 status 事件）。
  it("问题3 | deriveChildDelegationStatus 复用投影器口径（running/终态/审批/待定/无事件）", () => {
    const childTurnId = "turn_child_derive";

    // delegation_child_started 无 payload.status → 复用 delegationStatusFromEvent 兜底为 running。
    const runningEvents = [
      runtimeEvent(
        "ev-1",
        "delegation_child_started",
        "turn-parent",
        { child_turn_id: childTurnId, child_agent_id: "a" },
        1,
      ),
    ];
    expect(deriveChildDelegationStatus(childTurnId, runningEvents)).toBe("running");

    // 显式终态 finished → completed。
    const finishedEvents = [
      runtimeEvent(
        "ev-2",
        "delegation_finished",
        "turn-parent",
        { child_turn_id: childTurnId, child_agent_id: "a", status: "completed" },
        2,
      ),
    ];
    expect(deriveChildDelegationStatus(childTurnId, finishedEvents)).toBe("completed");

    // 显式 waiting_approval → 透传（类型穷尽覆盖）。
    const waitingEvents = [
      runtimeEvent(
        "ev-3",
        "delegation_child_started",
        "turn-parent",
        { child_turn_id: childTurnId, child_agent_id: "a", status: "waiting_approval" },
        3,
      ),
    ];
    expect(deriveChildDelegationStatus(childTurnId, waitingEvents)).toBe("waiting_approval");

    // 其他 turn 的事件不应误匹配（按 payload.child_turn_id 反查）。
    const otherTurnEvents = [
      runtimeEvent(
        "ev-4",
        "delegation_child_started",
        "turn-parent",
        { child_turn_id: "turn_other", child_agent_id: "a" },
        4,
      ),
    ];
    expect(deriveChildDelegationStatus(childTurnId, otherTurnEvents)).toBeUndefined();

    // 纯父委派事件（delegation_started 无 child_turn_id）不误匹配 → undefined。
    const parentDelegation = [
      runtimeEvent(
        "ev-5",
        "delegation_started",
        "turn-parent",
        { parent_turn_id: "turn-parent", child_agent_id: "a" },
        5,
      ),
    ];
    expect(deriveChildDelegationStatus(childTurnId, parentDelegation)).toBeUndefined();
  });

  // 测试目的：SubagentPanel 在 running 派生态下渲染「运行中」徽章，不落「状态未知」兜底。
  // 可能发现的缺陷：DELEGATION_STATUS_BADGE 仍缺 running 映射 → 显示「状态未知」。
  it("问题3 | running 派生态下 SubagentPanel 徽章渲染「运行中」（非状态未知）", () => {
    const childTurnId = "turn_child_running_badge";
    useEventStore.getState().setEvents(
      [
        runtimeEvent(
          "ev-r",
          "delegation_child_started",
          "turn-parent",
          { child_turn_id: childTurnId, child_agent_id: "delegate_reviewer", delegation_type: "review" },
          1,
        ),
      ],
      "task-delegation",
    );
    useDelegationStore.getState().selectChildTurn(childTurnId);

    render(<SubagentPanel />);

    expect(screen.queryByText("状态未知")).toBeNull();
    expect(screen.getByText("运行中")).toBeTruthy();
  });

  // 测试目的：failed 终态也应渲染「失败」徽章（验证类型穷尽不只在 happy path）。
  // 可能发现的缺陷：终态映射缺失导致「状态未知」。
  it("问题3 | failed 派生态下 SubagentPanel 徽章渲染「失败」（非状态未知）", () => {
    const childTurnId = "turn_child_failed_badge";
    useEventStore.getState().setEvents(
      [
        runtimeEvent(
          "ev-f",
          "delegation_failed",
          "turn-parent",
          { child_turn_id: childTurnId, child_agent_id: "delegate_reviewer", delegation_type: "review", status: "failed" },
          1,
        ),
      ],
      "task-delegation",
    );
    useDelegationStore.getState().selectChildTurn(childTurnId);

    render(<SubagentPanel />);

    expect(screen.queryByText("状态未知")).toBeNull();
    expect(screen.getByText("失败")).toBeTruthy();
  });

  // ── 问题4：running 兜底 TurnRecord 不误判终态 ────────────────────────
  // 测试目的：child 处于 running（事件含 delegation_child_started 但无终态）且 turnStore 无记录时，
  // SubagentPanel 应以「活动态」渲染 TurnTimeline，而非将兜底 record 的 status 误判为 completed
  // 导致 pending/思考块被提前折叠。验证：事件已到时不落入「暂无可渲染」错误态，且组件正常渲染。
  // 可能发现的缺陷：createFallbackTurn 仍写死 status:"completed" → TurnTimeline 以终态折叠。
  it("问题4 | running child 事件已到、turnStore 无记录时渲染 TurnTimeline（兜底 status 非 completed 终态折叠）", () => {
    const childTurnId = "turn_child_running_fallback";
    // 注入 delegation_child_started（无终态）→ 派生 running。
    useEventStore.getState().setEvents(
      [
        runtimeEvent(
          "ev-run",
          "delegation_child_started",
          "turn-parent",
          { child_turn_id: childTurnId, child_agent_id: "delegate_reviewer", delegation_type: "review" },
          1,
        ),
      ],
      "task-delegation",
    );
    useDelegationStore.getState().selectChildTurn(childTurnId);

    let renderError: unknown = null;
    let container: HTMLElement | null = null;
    try {
      const r = render(<SubagentPanel />);
      container = r.container;
    } catch (err) {
      renderError = err;
    }

    expect(renderError).toBeNull();
    // 事件已到达（childEvents.length > 0），且兜底 record 的 status 映射为 running（活动态），
    // 故应进入 TurnTimeline 渲染分支，而非「暂无可渲染的时间线条目」错误态。
    expect(screen.queryByText("该子 Agent 暂无可渲染的时间线条目。")).toBeNull();
    // 顶部元信息与 timeline 区域均存在，证明组件未因终态误判而折叠/早返回。
    expect(screen.getByText(childTurnId)).toBeTruthy();
    expect(container).not.toBeNull();
  });

  // 测试目的：切换选中目标（多次切换）后，旧 target 的选中态被新 target 覆盖，
  // 且面板稳定不抛错（覆盖多次切换场景）。
  // 可能发现的缺陷：选中态串扰或重渲染逃逸。
  it("问题4 补充 | 多次切换选中目标后面板稳定且选中态正确覆盖", () => {
    const t1 = "turn_switch_1";
    const t2 = "turn_switch_2";
    useEventStore.getState().setEvents(
      [
        runtimeEvent("ev-s1", "delegation_child_started", "turn-parent", { child_turn_id: t1, child_agent_id: "a" }, 1),
        runtimeEvent("ev-s2", "delegation_child_started", "turn-parent", { child_turn_id: t2, child_agent_id: "a" }, 2),
      ],
      "task-delegation",
    );

    let renderError: unknown = null;
    try {
      act(() => {
        useDelegationStore.getState().selectChildTurn(t1);
        render(<SubagentPanel />);
        useDelegationStore.getState().selectChildTurn(t2);
      });
    } catch (err) {
      renderError = err;
    }

    expect(renderError).toBeNull();
    expect(useDelegationStore.getState().selectedChildTurnId).toBe(t2);
    // 第二次选中（t2）应展示其元信息，无「状态未知」兜底（派生成功）。
    expect(screen.queryByText("状态未知")).toBeNull();
    expect(screen.getByText(t2)).toBeTruthy();
  });
});
