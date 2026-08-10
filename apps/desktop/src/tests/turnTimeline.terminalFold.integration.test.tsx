// @vitest-environment happy-dom
/**
 * 思考块终态折叠 —— 组件层 / 链路层独立验证测试。
 *
 * 与既有 `turnTimeline.terminalFold.test.ts`（纯函数层）的分工：
 * 纯函数测试只能证明 `selectVisibleEntries(state, false)` 会去掉 streaming 标记，
 * **无法证明** `TurnTimeline` 真的把 `turn.status` 正确翻译成 `isTurnActive`
 * 并在状态变化时重算 useMemo。若 `TurnTimeline.tsx` 把判定写反（如误用
 * `status !== "pending"`）、漏传第二参数、或 useMemo 依赖漏了 `turn.status`，
 * 纯函数测试**全部依然会绿**。本文件从 DOM 与 store 两个真实观察点补齐该缺口。
 *
 * 覆盖三层：
 * 1. 组件层：turn.status 翻转 → ThinkingBlock 收到的 streaming prop / 真实 DOM 变化；
 * 2. 链路层：SSE 异常断流 → useSSE.markFailed → turnStore 写入 failed；
 * 3. 纯度层：反复派生不得污染 projector 累积态。
 *
 * @module tests/turnTimeline.terminalFold.integration
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, act } from "@testing-library/react";
import type { RuntimeEvent } from "@shared/events";
import type { TurnRecord, TurnStatus } from "@shared/turn";
import {
  createTimelineProjectorState,
  projectTimelineIncrementally,
  selectVisibleEntries,
} from "@/services/timeline/projector";
import { TurnTimeline } from "@/components/layout/TurnTimeline";

// Tauri 宿主模块，测试环境不存在，mock 防止导入崩溃。
vi.mock("@/services/backend", () => ({
  openFileInEditor: vi.fn().mockResolvedValue(undefined),
}));

// 记录 ThinkingBlock 每次收到的 streaming prop —— 这是「折叠 / 展开」的唯一决定因素，
// 比断言 DOM class 更精确，且不受 ThinkingBlock 内部样式重构影响。
const thinkingProps: Array<{ content: string; streaming: boolean | undefined }> = [];

vi.mock("@/components/chat/ThinkingBlock", () => ({
  ThinkingBlock: ({ content, streaming }: { content: string; streaming?: boolean }) => {
    thinkingProps.push({ content, streaming });
    // 真实 ThinkingBlock 的行为契约：streaming 时无折叠栏、直接展开；
    // 非 streaming 时渲染「深度思考」折叠栏且默认收起。此处以最小 DOM 复刻该契约，
    // 使测试可在 DOM 层面断言「用户实际看到的是展开还是折叠」。
    return streaming ? (
      <div data-testid="thinking-expanded">{content}</div>
    ) : (
      <button type="button" data-testid="thinking-collapsed">
        深度思考
      </button>
    );
  },
}));

vi.mock("@/components/chat/UserMessage", () => ({
  UserMessage: ({ content }: { content: string }) => <div data-testid="user-msg">{content}</div>,
}));

const NOW = "2026-08-08T12:00:00.000Z";

/** 构造一条 thinking delta 事件（文本字段为 payload.text）。 */
function thinkingDelta(eventId: string, text: string, sequence = 1): RuntimeEvent {
  return {
    event_id: eventId,
    event_type: "model_thinking_delta",
    turn_id: "turn-1",
    task_id: "task-1",
    created_at: NOW,
    sequence,
    payload: { text },
  } as unknown as RuntimeEvent;
}

/** 构造一条正文 delta 事件。 */
function outputDelta(eventId: string, text: string, sequence = 1): RuntimeEvent {
  return {
    event_id: eventId,
    event_type: "model_output_delta",
    turn_id: "turn-1",
    task_id: "task-1",
    created_at: NOW,
    sequence,
    payload: { text },
  } as unknown as RuntimeEvent;
}

/** 构造一条工具事件（用于「pending 与已定稿条目混合」场景）。 */
function toolStarted(eventId: string, callId: string, sequence = 1): RuntimeEvent {
  return {
    event_id: eventId,
    event_type: "tool_call_started",
    turn_id: "turn-1",
    task_id: "task-1",
    created_at: NOW,
    sequence,
    payload: { tool_name: "read_file", tool_call_id: callId, arguments: { path: "a.ts" } },
  } as unknown as RuntimeEvent;
}

/**
 * 构造 turn 记录。
 *
 * 关键：每次调用返回**新对象引用**，模拟 zustand `updateTurn` 的不可变更新
 * （`{ ...turn, ...updates }`），这正是真实链路中 turn 引用变化的方式。
 */
function makeTurn(status: TurnStatus): TurnRecord {
  return {
    turn_id: "turn-1",
    task_id: "task-1",
    input_text: "帮我分析一下",
    status,
    end_reason: null,
    response_text: null,
    created_at: NOW,
    updated_at: NOW,
  } as unknown as TurnRecord;
}

beforeEach(() => {
  thinkingProps.length = 0;
});

describe("组件层：turn.status 翻转驱动思考块折叠（DOM 验证）", () => {
  it("running → failed：思考块从展开变为折叠（核心 bug 回归护栏）", () => {
    // 测试目的：验证「异常断流」这条主链路在组件层真正闭合。
    // 可能发现的缺陷：isTurnActive 判定写反 / 漏传第二参数 / useMemo 漏依赖 turn.status
    //   → 三者中任意一个出错，思考块都会在 failed 后仍保持展开（原 bug 复现）。
    const events = [thinkingDelta("e1", "我先分析一下这个问题")];

    const { rerender, queryByTestId } = render(
      <TurnTimeline turn={makeTurn("running")} events={events} />,
    );

    // 断流前：running → 展开态（streaming 生效）
    expect(queryByTestId("thinking-expanded")).not.toBeNull();
    expect(queryByTestId("thinking-collapsed")).toBeNull();
    expect(thinkingProps[thinkingProps.length - 1]?.streaming).toBe(true);

    // 断流：终态事件一条都没到，events 数组引用完全不变，
    // 仅 turn 被 markFailed 改写为 failed（新引用）。
    act(() => {
      rerender(<TurnTimeline turn={makeTurn("failed")} events={events} />);
    });

    // 断流后：必须折叠。DOM 层面验证，而非仅纯函数。
    expect(queryByTestId("thinking-collapsed")).not.toBeNull();
    expect(queryByTestId("thinking-expanded")).toBeNull();
    // streaming 必须是 undefined（缺省）而非 false，与 flushPending 定稿条目形状一致。
    expect(thinkingProps[thinkingProps.length - 1]?.streaming).toBeUndefined();
  });

  it("events 引用不变、仅 turn.status 变化时 useMemo 必须重算（依赖项护栏）", () => {
    // 测试目的：锁死 useMemo 依赖数组包含 turn.status。
    // 可能发现的缺陷：依赖数组写成 [renderTick] 或仅 [turn]（若上层复用同一 turn 对象引用），
    //   会导致 status 已变但 entries 仍是旧的展开态缓存值 —— 这是最隐蔽的一类漏改。
    const events = [thinkingDelta("e1", "思考内容")];
    const { rerender, queryByTestId } = render(
      <TurnTimeline turn={makeTurn("running")} events={events} />,
    );
    expect(queryByTestId("thinking-expanded")).not.toBeNull();

    const callsBefore = thinkingProps.length;
    act(() => {
      // 完全相同的 events 引用；只有 turn 变了。useEffect 不会跑（无 delta），
      // 因此 renderTick 不变 —— 重算只能靠 turn/turn.status 依赖触发。
      rerender(<TurnTimeline turn={makeTurn("failed")} events={events} />);
    });

    expect(thinkingProps.length).toBeGreaterThan(callsBefore);
    expect(queryByTestId("thinking-collapsed")).not.toBeNull();
  });

  it.each<TurnStatus>(["completed", "cancelled", "reverted", "failed"])(
    "终态 %s 下残留 pending 思考块一律折叠",
    (status) => {
      // 测试目的：覆盖 TurnStatus 全部终态取值，尤其是 reverted
      //   —— 若实现写成 `status !== "failed" && status !== "cancelled"` 之类的
      //   「黑名单枚举」而非「白名单 pending|running」，reverted / completed 会漏网。
      // 可能发现的缺陷：终态判定用黑名单枚举导致新增状态漏折叠。
      const { queryByTestId } = render(
        <TurnTimeline turn={makeTurn(status)} events={[thinkingDelta("e1", "思考内容")]} />,
      );
      expect(queryByTestId("thinking-collapsed")).not.toBeNull();
      expect(queryByTestId("thinking-expanded")).toBeNull();
    },
  );

  it.each<TurnStatus>(["pending", "running"])("活动态 %s 下 pending 思考块保持展开", (status) => {
    // 测试目的：反向护栏 —— 防止「一律折叠」式过度修复。
    // 可能发现的缺陷：若把 isTurnActive 恒定写死为 false，流式期思考过程将不可见，
    //   用户在生成期间看不到任何进展（体验回退），而所有终态用例仍会绿。
    const { queryByTestId } = render(
      <TurnTimeline turn={makeTurn(status)} events={[thinkingDelta("e1", "思考内容")]} />,
    );
    expect(queryByTestId("thinking-expanded")).not.toBeNull();
    expect(queryByTestId("thinking-collapsed")).toBeNull();
  });

  it("快速多次状态翻转 running→failed→running→completed 每次都正确响应", () => {
    // 测试目的：验证折叠是「由当前 status 纯派生」而非一次性副作用。
    // 可能发现的缺陷：若实现用 ref/useState 记忆「已折叠过」，重回 running 将无法恢复展开
    //   （重试 / 续跑场景下思考过程永久不可见）。
    const events = [thinkingDelta("e1", "思考内容")];
    const { rerender, queryByTestId } = render(
      <TurnTimeline turn={makeTurn("running")} events={events} />,
    );
    expect(queryByTestId("thinking-expanded")).not.toBeNull();

    const sequence: Array<[TurnStatus, "expanded" | "collapsed"]> = [
      ["failed", "collapsed"],
      ["running", "expanded"],
      ["completed", "collapsed"],
      ["pending", "expanded"],
      ["cancelled", "collapsed"],
    ];

    for (const [status, expected] of sequence) {
      act(() => {
        rerender(<TurnTimeline turn={makeTurn(status)} events={events} />);
      });
      if (expected === "expanded") {
        expect(queryByTestId("thinking-expanded"), `status=${status} 应展开`).not.toBeNull();
        expect(queryByTestId("thinking-collapsed"), `status=${status} 不应折叠`).toBeNull();
      } else {
        expect(queryByTestId("thinking-collapsed"), `status=${status} 应折叠`).not.toBeNull();
        expect(queryByTestId("thinking-expanded"), `status=${status} 不应展开`).toBeNull();
      }
    }
  });

  it("turn 停在 running（异常断流未成功写终态时）思考块仍展开 —— 修复的失效边界", () => {
    // 测试目的：界定本次修复的作用范围。折叠**完全依赖** turn.status 已被写成终态；
    //   一旦上游链路没能把 status 改掉，本次改动无法生效。
    // 可能发现的缺陷：与 useSSE.abnormalStreamEnd 用例联读可知——异常断流时 markFailed
    //   写入的 failed 会被迟到的 run_started flush 覆盖回 running，
    //   于是真实场景下这里的 running 分支才是实际生效路径，原 bug 依然可复现。
    const { queryByTestId } = render(
      <TurnTimeline turn={makeTurn("running")} events={[thinkingDelta("e1", "被中断的思考")]} />,
    );
    expect(queryByTestId("thinking-expanded")).not.toBeNull();
    expect(queryByTestId("thinking-collapsed")).toBeNull();
  });

  it("终态折叠不吞掉思考内容：文本仍完整保留在条目中", () => {
    // 测试目的：确保「折叠」是视觉收起，而不是把 pending 内容丢弃。
    // 可能发现的缺陷：若实现改成「终态时不推入 pending 块」，思考内容会彻底消失，
    //   而只断言「没有展开态 DOM」的弱测试同样会绿 —— 本用例堵住该假阳性。
    const events = [thinkingDelta("e1", "第一段推理，"), thinkingDelta("e2", "第二段推理。", 2)];
    render(<TurnTimeline turn={makeTurn("failed")} events={events} />);

    expect(thinkingProps[thinkingProps.length - 1]?.content).toBe("第一段推理，第二段推理。");
  });
});

describe("边界：pending 与已定稿条目混合", () => {
  it("已定稿 thinking + 残留 pending 正文，终态下两者形状一致（均无 streaming）", () => {
    // 测试目的：thinking→output 切换会让 thinking 被 flushPending 定稿，output 仍 pending；
    //   终态下二者应无法从形状上区分。
    // 可能发现的缺陷：定稿条目与派生 pending 条目形状分叉，下游出现两种「非流式」表示，
    //   导致 memo 比较或条件渲染出现不一致。
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      thinkingDelta("e1", "先思考"),
      outputDelta("e2", "再回答", 2),
    ]);

    const folded = selectVisibleEntries(state, false);
    expect(folded).toHaveLength(2);
    expect(folded[0]).toEqual({ kind: "thinking", eventId: "e1", content: "先思考" });
    // 精确断言整个对象：streaming 键必须完全不存在，而非值为 undefined。
    expect(Object.keys(folded[1] as object).sort()).toEqual(["content", "eventId", "kind"]);
  });

  it("工具条目（已定稿）+ pending 思考块混合：工具条目不受终态派生影响", () => {
    // 测试目的：终态派生只应作用于 pending 块，不得触碰已定稿的 tool/status 条目。
    // 可能发现的缺陷：若实现对整个 entries 数组做 map 剥离 streaming，会产生新引用，
    //   击穿下游 memo（性能回退），且可能误改工具条目字段。
    let state = projectTimelineIncrementally(createTimelineProjectorState(), [
      toolStarted("t1", "call-1"),
    ]);
    const finalizedTool = state.entries[0];

    state = projectTimelineIncrementally(state, [thinkingDelta("e2", "工具跑完后继续想", 2)]);

    const folded = selectVisibleEntries(state, false);
    expect(folded).toHaveLength(2);
    // 已定稿工具条目必须是**同一引用**（引用稳定契约 / memo 前提）。
    expect(folded[0]).toBe(finalizedTool);
    expect(folded[1]).toMatchObject({ kind: "thinking", content: "工具跑完后继续想" });
  });

  it("无 pending 时终态派生直接返回原数组引用（零分配契约）", () => {
    // 测试目的：锁住「无 pending 时零分配」的性能契约，两种 isTurnActive 都应成立。
    // 可能发现的缺陷：新增参数后若把早返回改成无条件 slice()，每帧都产生新数组引用，
    //   击穿 TurnTimeline 下游所有 memo。
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      toolStarted("t1", "call-1"),
    ]);
    expect(selectVisibleEntries(state, true)).toBe(state.entries);
    expect(selectVisibleEntries(state, false)).toBe(state.entries);
  });
});

describe("纯度：反复派生不得污染 projector 累积状态", () => {
  it("多次调用 selectVisibleEntries 不改变 state，且结果稳定可重复", () => {
    // 测试目的：折叠是纯派生。若就地把 pending 置 null 或写回 entries，
    //   后端补发迟到事件 / 用户重连时 pending 会被错误吞掉。
    // 可能发现的缺陷：selectVisibleEntries 产生副作用（修改 pendingThinking/entries/pendingDelta）。
    // 注意：仅投影 thinking delta，使 pendingThinking 保持 pending 状态
    //   （一旦跟随 output delta，projector 会把 thinking 定稿进 entries 并置空）。
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      thinkingDelta("e1", "思考中"),
    ]);

    const entriesRefBefore = state.entries;
    const entriesLenBefore = state.entries.length;
    const thinkingBefore = state.pendingThinking;
    expect(thinkingBefore?.content).toBe("思考中");

    // 交替、反复派生 10 次
    for (let i = 0; i < 10; i += 1) {
      selectVisibleEntries(state, i % 2 === 0);
    }

    expect(state.entries).toBe(entriesRefBefore);
    expect(state.entries).toHaveLength(entriesLenBefore);
    expect(state.pendingThinking).toBe(thinkingBefore);
    expect(state.pendingThinking?.content).toBe("思考中");
    expect(state.pendingDelta).toBeNull();

    // 幂等：终态派生结果内容恒定（不会层层堆叠 / 被吞）
    const a = selectVisibleEntries(state, false);
    const b = selectVisibleEntries(state, false);
    expect(a).toEqual(b);
    expect(a).toHaveLength(1);
    expect(a[0]).toEqual({ kind: "thinking", eventId: "e1", content: "思考中" });
  });

  it("pendingDelta 残留场景下反复派生同样无副作用", () => {
    // 测试目的：补齐 assistant 分支的纯度覆盖（上一用例只覆盖 thinking 分支）。
    // 可能发现的缺陷：assistant 分支就地修改 pendingDelta 或把派生条目写回 entries。
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      thinkingDelta("e1", "先思考"),
      outputDelta("e2", "再回答", 2),
    ]);
    // thinking 已被 output 中断而定稿；此时只剩 pendingDelta 仍在累积。
    expect(state.pendingThinking).toBeNull();
    const deltaBefore = state.pendingDelta;
    const entriesRefBefore = state.entries;
    expect(deltaBefore?.content).toBe("再回答");

    for (let i = 0; i < 10; i += 1) {
      selectVisibleEntries(state, i % 2 === 0);
    }

    expect(state.pendingDelta).toBe(deltaBefore);
    expect(state.pendingDelta?.content).toBe("再回答");
    expect(state.entries).toBe(entriesRefBefore);
    // 定稿 thinking 恒为 1 条，不因反复派生而堆叠。
    expect(state.entries.filter((e) => e.kind === "thinking")).toHaveLength(1);
  });

  it("终态派生后仍能被后续增量投影正常覆盖（迟到事件不丢）", () => {
    // 测试目的：折叠只是视图态；若后端稍后补发终态事件，累积态必须仍可正常 flush。
    // 可能发现的缺陷：派生阶段污染 accumulator，导致迟到的 run_failed 到达时
    //   pending 内容重复入 entries（"思考"出现两次）或彻底丢失。
    let state = projectTimelineIncrementally(createTimelineProjectorState(), [
      thinkingDelta("e1", "被中断的思考"),
    ]);

    // 断流后 UI 已按终态折叠渲染多帧
    selectVisibleEntries(state, false);
    selectVisibleEntries(state, false);

    // 迟到的终态事件补发
    const runFailed = {
      event_id: "e9",
      event_type: "run_failed",
      turn_id: "turn-1",
      task_id: "task-1",
      created_at: NOW,
      sequence: 9,
      payload: { error: "backend crashed" },
    } as unknown as RuntimeEvent;
    state = projectTimelineIncrementally(state, [runFailed]);

    // pending 被正常 flush 成唯一一条定稿 thinking，不重复、不丢失
    const thinkingEntries = state.entries.filter((e) => e.kind === "thinking");
    expect(thinkingEntries).toHaveLength(1);
    expect(thinkingEntries[0]).toMatchObject({ content: "被中断的思考" });
    expect(state.pendingThinking).toBeNull();
  });
});
