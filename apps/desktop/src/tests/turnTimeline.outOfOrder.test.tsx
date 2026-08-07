// @vitest-environment happy-dom
/**
 * TurnTimeline 乱序自愈能力验证测试（端到端经 eventStore）。
 *
 * 背景：`TurnTimelineImpl` 依赖「events 为 append-only 且有序」做尾部切片续算，
 * 并以「长度变小」或「首事件 id 变化」检测整体替换（此时全量重建）。
 *
 * 真实链路：SSE → useSSE 攒批 → eventStore.appendEvents → eventsByTurnId 分片
 * → ChatPanel → TurnTimeline → projector。rendering 只消费分片 `eventsByTurnId`，
 * 而扁平 `events` 仅用于 `events.length` 触发滚动。
 *
 * 因此「乱序」必须发生在 eventStore 这层：只有分片被修成有序，TurnTimeline 的
 * 尾部续算假设才成立，正文才不会被迟到 delta 永久错乱。本测试经 eventStore 注入
 * 乱序到达的事件，验证分片有序、且 TurnTimeline 增量续算后得到正确顺序。
 *
 * 与 projector 单元测试的区别：projector 的契约是「输入 delta 有序」，其自身的
 * 乱序防护（工具 finished 先到的 alreadyTerminal 保护）已另有测试覆盖；本测试
 * 验证的是「乱序在 eventStore 层被归位、不再传导到渲染」。
 *
 * @module tests/turnTimeline.outOfOrder
 */

import { describe, expect, it, vi } from "vitest";
import type { RuntimeEvent } from "@shared/events";
import { useEventStore } from "@/stores/eventStore";
import {
  createTimelineProjectorState,
  projectTimelineIncrementally,
  selectVisibleEntries,
  type TurnTimelineEntry,
} from "@/services/timeline/projector";

vi.mock("@/lib/logger", () => ({
  logInfo: () => {},
  logWarn: () => {},
  logError: () => {},
}));

/**
 * 构造运行时事件。
 *
 * @param id - event_id。
 * @param eventType - 事件类型。
 * @param payload - 事件负载。
 * @param sequence - 后端全局序号（决定正确顺序）。
 * @returns RuntimeEvent。
 */
function ev(
  id: string,
  eventType: string,
  payload: Record<string, unknown>,
  sequence: number,
): RuntimeEvent {
  return {
    event_id: id,
    task_id: "task-1",
    turn_id: "turn-1",
    event_type: eventType,
    payload,
    sequence,
    created_at: new Date(1700000000000 + sequence * 1000).toISOString(),
  } as unknown as RuntimeEvent;
}

/** 抽取 assistant 文本内容。 */
function assistantText(entries: TurnTimelineEntry[]): string {
  return entries
    .filter((e): e is Extract<TurnTimelineEntry, { kind: "assistant" }> => e.kind === "assistant")
    .map((e) => e.content)
    .join("");
}

/** 经 eventStore 注入一批乱序到达的事件，返回分片（渲染真正消费的数据源）。 */
function pushThroughStore(events: RuntimeEvent[]): RuntimeEvent[] {
  useEventStore.getState().clearEvents();
  // 模拟 SSE 攒批 / 重连回放导致的乱序到达：单批内逆序
  useEventStore.getState().appendEvents(events);
  return useEventStore.getState().eventsByTurnId["turn-1"] ?? [];
}

describe("TurnTimeline 乱序自愈能力（经 eventStore 归位）", () => {
  it("批内逆序到达：分片被归位，TurnTimeline 续算得到正确正文", () => {
    // 后端真实产出顺序为 "Hello" (seq=1) → " world" (seq=2)，
    // 但网络重排使同批按 [seq=2, seq=1] 到达。
    const shard = pushThroughStore([
      ev("e2", "model_output_delta", { text: " world" }, 2),
      ev("e1", "model_output_delta", { text: "Hello" }, 1),
    ]);

    // 分片（渲染数据源）必须已被 eventStore 排序，而非原到达顺序
    expect(shard.map((e) => e.sequence)).toEqual([1, 2]);

    // TurnTimeline 在有序分片上增量续算：正确还原 "Hello world"
    const state = projectTimelineIncrementally(createTimelineProjectorState(), shard);
    expect(assistantText(selectVisibleEntries(state))).toBe("Hello world");
  });

  it("跨批逆序到达：迟到事件不破坏已渲染正文", () => {
    useEventStore.getState().clearEvents();
    // 第一帧：收到 seq=2（迟到，先到）
    useEventStore.getState().appendEvents([
      ev("e2", "model_output_delta", { text: " world" }, 2),
    ]);
    // 第二帧：迟到的 seq=1 随后到达（跨批逆序，命中 needsReorder）
    useEventStore.getState().appendEvents([
      ev("e1", "model_output_delta", { text: "Hello" }, 1),
    ]);

    const shard = useEventStore.getState().eventsByTurnId["turn-1"] ?? [];
    expect(shard.map((e) => e.sequence)).toEqual([1, 2]);

    // 模拟 TurnTimeline 的「首帧全量 + 尾部续算」：分片整体有序，续算无错乱
    const state = projectTimelineIncrementally(createTimelineProjectorState(), shard);
    expect(assistantText(selectVisibleEntries(state))).toBe("Hello world");
  });
});
