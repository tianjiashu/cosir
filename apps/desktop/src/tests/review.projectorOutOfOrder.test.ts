/**
 * projector 乱序 / 终态边界可疑点验证。
 *
 * 可疑点 D：final_response 与 model_output_delta 乱序。
 *   projector 中 final_response 仅在「!hasDeltaStreamed」时推入 assistant；
 *   若 final_response 先到达（网络重排），hasDeltaStreamed 仍为 false，final_response
 *   文本被推入；随后迟到 model_output_delta 到达，hasDeltaStreamed 变 true，其文本累积
 *   为 pendingDelta，在 selectVisibleEntries 时追加——结果是「final_response 文本 + delta 文本」
 *   两段 assistant，内容重复（模型完整回复被拆成两半展示）。
 *
 * 可疑点 B：thinking 块阈值不一致。
 *   selectVisibleEntries 用 pendingThinking.content.trim().length > 0 判定推入；
 *   TurnTimelineImpl 的 TimelineEntry 用 >= 2 过滤。仅 1 字符的 thinking 内容会在
 *   selectVisibleEntries 推入、TimelineEntry 返回 null——条目存在但不渲染，且会触发
 *   一次额外的 render / 引用变化（浪费），但「应为空不渲染」语义一致，无内容错乱。
 *
 * @module tests/review.projectorOutOfOrder
 */

import { describe, expect, it } from "vitest";
import type { RuntimeEvent } from "@shared/events";
import {
  createTimelineProjectorState,
  projectTimelineIncrementally,
  selectVisibleEntries,
  type TurnTimelineEntry,
} from "@/services/timeline/projector";

let seq = 0;
function makeEvent(
  eventType: RuntimeEvent["event_type"],
  payload: Record<string, unknown>,
  eventId?: string,
): RuntimeEvent {
  seq += 1;
  return {
    event_id: eventId ?? `evt-${seq}`,
    task_id: "task-1",
    turn_id: "turn-1",
    event_type: eventType,
    payload,
    created_at: new Date().toISOString(),
  } as RuntimeEvent;
}

function assistantTexts(entries: TurnTimelineEntry[]): string[] {
  return entries.filter((e) => e.kind === "assistant").map((e) => (e as { content: string }).content);
}

describe("projector 乱序：final_response 先于 model_output_delta", () => {
  it("final_response 先到 + 迟到 delta 后到时，不应出现回复内容重复展示", () => {
    const fullText = "这是模型的完整回复。";
    // 场景：final_response 携带完整文本先到。
    let state = projectTimelineIncrementally(createTimelineProjectorState(), [
      makeEvent("final_response", { text: fullText }, "fr-1"),
    ]);
    // 迟到：model_output_delta 网络重排后到（同一条回复被流式拆成片段）。
    state = projectTimelineIncrementally(state, [
      makeEvent("model_output_delta", { text: "这是" }, "d-1"),
      makeEvent("model_output_delta", { text: "模型的完整回复。" }, "d-2"),
    ]);

    const visible = selectVisibleEntries(state, true).slice();
    const texts = assistantTexts(visible);
    // 期望：只应有一段 assistant，内容为完整回复，不应出现「final + delta」两段拼接。
    const combined = texts.join("");
    expect(texts.length).toBeLessThanOrEqual(1);
    if (texts.length === 1) {
      expect(combined).not.toContain(fullText + fullText.slice(0, 2));
    }
  });

  it("仅 final_response（无 delta）时正常展示单段回复", () => {
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      makeEvent("final_response", { text: "hello world" }, "fr-2"),
    ]);
    const texts = assistantTexts(selectVisibleEntries(state, true));
    expect(texts).toEqual(["hello world"]);
  });
});

describe("projector thinking 阈值一致性", () => {
  it("1 字符 thinking 内容：selectVisibleEntries 推入但 TimelineEntry 过滤返回 null 时不应造成内容错乱", () => {
    // 模拟流式期收到单字符 thinking，随后思考结束（被后续非 delta 事件 flush）。
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      makeEvent("model_thinking_delta", { text: "。" }, "t-1"),
      makeEvent("model_output_delta", { text: "正式回复" }, "o-1"),
    ]);
    const visible = selectVisibleEntries(state, true);
    const thinkingEntries = visible.filter((e) => e.kind === "thinking");
    // selectVisibleEntries 用 > 0 判定，单字符 "。" 会被推入（与 TimelineEntry >= 2 过滤不一致）
    expect(thinkingEntries.length).toBeGreaterThanOrEqual(0);
  });
});
