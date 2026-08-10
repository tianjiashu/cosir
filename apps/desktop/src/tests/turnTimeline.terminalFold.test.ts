// @vitest-environment happy-dom
/**
 * TurnTimeline 终态折叠契约测试。
 *
 * 背景 bug：模型正在吐 thinking → turn 进入终态（典型场景：客户端 SSE 断开 →
 * 后端 finally 把 turn 标 failed 落库，但不再投递 run_* 终态 event 包） →
 * `pendingThinking` 残留，selectVisibleEntries 持续以 streaming:true 派生，
 * ThinkingBlock 永久展开 + 与后续 turn 渲染区重叠挤压空间。
 *
 * 修复契约：`selectVisibleEntries(state, isTurnActive)`。
 * turn 已进入终态时，pending 块以 `streaming` 缺省推入（按定稿态渲染），
 * accumulator 不动，等待后续增量投影自然覆盖。
 *
 * 本文件用纯函数测试覆盖该契约，不渲染组件，避免 happy-dom 对
 * `useRef`/`useEffect`/异步态的副作用放大；集成行为由 useTask.openTask
 * 等组件测试覆盖历史回放路径。
 */
import { describe, expect, it } from "vitest";
import {
  createTimelineProjectorState,
  projectTimelineIncrementally,
  selectVisibleEntries,
} from "@/services/timeline/projector";
import type { RuntimeEvent } from "@shared/events";

const NOW = "2026-08-08T12:00:00.000Z";

/** 构造一条 thinking delta 事件（projectTimelineIncrementally 的最小契约）。 */
function thinkingDelta(eventId: string, text: string, turnId = "turn-1"): RuntimeEvent {
  return {
    event_id: eventId,
    event_type: "model_thinking_delta",
    turn_id: turnId,
    task_id: "task-1",
    created_at: NOW,
    sequence: Number(eventId.replace(/\D/g, "")) || 0,
    payload: { text },
  } as unknown as RuntimeEvent;
}

/** 构造一条正文输出 delta 事件。 */
function outputDelta(eventId: string, text: string, turnId = "turn-1"): RuntimeEvent {
  return {
    event_id: eventId,
    event_type: "model_output_delta",
    turn_id: turnId,
    task_id: "task-1",
    created_at: NOW,
    sequence: Number(eventId.replace(/\D/g, "")) || 0,
    payload: { text },
  } as unknown as RuntimeEvent;
}

/** 投影若干 thinking delta，得到带有 pendingThinking 的状态。 */
function projectPendingThinking(events: RuntimeEvent[]) {
  return projectTimelineIncrementally(createTimelineProjectorState(), events);
}

describe("selectVisibleEntries - 终态折叠契约", () => {
  it("turn 活动态：pending 块以 streaming:true 推入", () => {
    const state = projectPendingThinking([thinkingDelta("e1", "正在分析…")]);
    const visible = selectVisibleEntries(state, true);
    expect(visible).toHaveLength(1);
    expect(visible[0]).toMatchObject({
      kind: "thinking",
      content: "正在分析…",
      streaming: true,
    });
  });

  it("turn 终态：pending 块以 streaming 缺省推入（折叠态，不带 caret）", () => {
    const state = projectPendingThinking([thinkingDelta("e1", "正在分析…")]);
    const visible = selectVisibleEntries(state, false);
    expect(visible).toHaveLength(1);
    expect(visible[0]).toMatchObject({
      kind: "thinking",
      content: "正在分析…",
    });
    // 关键断言：streaming 字段必须缺省（而非 false），保持与 flushPending 定稿条目标记一致。
    expect((visible[0] as { streaming?: boolean }).streaming).toBeUndefined();
  });

  it("默认参数 isTurnActive=true（向后兼容 4 个既有调用点）", () => {
    const state = projectPendingThinking([thinkingDelta("e1", "向后兼容")]);
    const visible = selectVisibleEntries(state);
    expect(visible[0]).toMatchObject({
      kind: "thinking",
      streaming: true,
    });
  });

  it("空 state（无任何 pending）在活动/终态下都返回空条目", () => {
    const state = createTimelineProjectorState();
    expect(selectVisibleEntries(state, true)).toEqual([]);
    expect(selectVisibleEntries(state, false)).toEqual([]);
  });

  it("pendingThinking 仅含空白字符时不推入可见条目（覆盖 hasThinking 的 trim 分支）", () => {
    // 与「空 state」不同：此处 pendingThinking 确实存在，但 content 全为空白，
    // 应被 trim 判定过滤掉，不产生一个空的思考块。
    const state = projectPendingThinking([thinkingDelta("e1", "   \n  ")]);
    expect(state.pendingThinking).not.toBeNull();
    expect(selectVisibleEntries(state, true)).toEqual([]);
    expect(selectVisibleEntries(state, false)).toEqual([]);
  });

  it("跨多 turn 历史回放场景：终态 turn 的残留 pending 折叠，新 turn 仍展开", () => {
    // 上一轮 turn 已结束但 projector 残留 pending（flush 缺失），
    // 下一轮 turn 重新开流，本测试验证：投影两个 turn 的事件，
    // 模拟「老 turn 终态 + 新 turn 活动」的双视角。
    const oldTurn = projectPendingThinking([thinkingDelta("e1", "上一轮思考", "old-turn")]);
    const newTurn = projectPendingThinking([thinkingDelta("e2", "新一轮思考", "new-turn")]);

    // 终态视角：两个 turn 的 pending 都折叠（各自被渲染时按所属 turn 状态决定）
    expect(selectVisibleEntries(oldTurn, false)[0]).toMatchObject({ kind: "thinking", content: "上一轮思考" });
    expect((selectVisibleEntries(oldTurn, false)[0] as { streaming?: boolean }).streaming).toBeUndefined();

    // 活动视角：新 turn 仍展开
    expect(selectVisibleEntries(newTurn, true)[0]).toMatchObject({
      kind: "thinking",
      content: "新一轮思考",
      streaming: true,
    });
  });

  it("异常断流（后端无任何终态事件）：仅凭 turn.status 转 failed 即可折叠", () => {
    // 本用例锁住「思考块异常中断」这条最关键的链路语义：
    //   后端崩溃/断流，run_failed 等终态事件一条都没到 →
    //   SSEConnection._reportStreamEndIfAbnormal 检测到「流结束但 _terminalReceived=false」→
    //   onError → useSSE.markFailed → updateTurn(status:"failed") →
    //   TurnTimeline 的 useMemo 依赖 turn.status 重算 → isTurnActive=false → 折叠。
    // 即：折叠不依赖后端投递中断事件，前端凭 SSE 自愈判定即可闭合。
    const state = projectPendingThinking([
      thinkingDelta("e1", "我先分析一下这个问题，"),
      thinkingDelta("e2", "然后再决定用哪个方案——"),
    ]);

    // 断流前（turn 仍是 running）：展开 + caret
    const streamingView = selectVisibleEntries(state, true);
    expect(streamingView[0]).toMatchObject({ kind: "thinking", streaming: true });

    // 断流后（markFailed 已把 turn.status 写成 failed，事件流没有任何新增）：
    // 同一份 projector 状态、同一个残留 pending，仅凭 isTurnActive 翻转即折叠。
    const foldedView = selectVisibleEntries(state, false);
    expect(foldedView).toHaveLength(1);
    expect(foldedView[0]).toMatchObject({
      kind: "thinking",
      content: "我先分析一下这个问题，然后再决定用哪个方案——",
    });
    expect((foldedView[0] as { streaming?: boolean }).streaming).toBeUndefined();

    // 关键回归护栏：折叠是**派生行为**，不得就地篡改 projector 累积状态，
    // 否则一旦后端补发迟到事件或用户重连，pending 会被错误吞掉。
    expect(state.pendingThinking?.content).toBe("我先分析一下这个问题，然后再决定用哪个方案——");
  });

  it("pendingDelta（正文输出）在终态下同样以 streaming 缺省推入", () => {
    // 改动同时影响 assistant delta 分支，需与 thinking 分支同等覆盖：
    // 正文吐到一半断流时，同样不应残留 caret / 流式态。
    const state = projectPendingThinking([outputDelta("e1", "根据分析，结论是")]);

    expect(selectVisibleEntries(state, true)[0]).toMatchObject({
      kind: "assistant",
      content: "根据分析，结论是",
      streaming: true,
    });

    const folded = selectVisibleEntries(state, false);
    expect(folded).toHaveLength(1);
    expect(folded[0]).toMatchObject({ kind: "assistant", content: "根据分析，结论是" });
    expect((folded[0] as { streaming?: boolean }).streaming).toBeUndefined();
  });

  it("终态下 pending 条目与 flushPending 定稿条目形状一致（均无 streaming 字段）", () => {
    // thinking → output 的切换会让 thinking 被 flushPending 定稿入 entries，
    // 而 output 仍是 pending。终态下二者应当无法从形状上区分，
    // 否则下游会出现「定稿条目有两种形状」的隐性分叉。
    const state = projectPendingThinking([
      thinkingDelta("e1", "先思考"),
      outputDelta("e2", "再回答"),
    ]);

    const folded = selectVisibleEntries(state, false);
    expect(folded).toHaveLength(2);
    expect(folded[0]).toMatchObject({ kind: "thinking", content: "先思考" });
    expect(folded[1]).toMatchObject({ kind: "assistant", content: "再回答" });
    for (const entry of folded) {
      expect((entry as { streaming?: boolean }).streaming).toBeUndefined();
    }
  });
});