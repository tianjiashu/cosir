/**
 * timeline 工具条目聚合。
 *
 * 把投影器产出的「连续 tool 段」在渲染层聚合为 toolGroup，用于解决同 turn 多工具
 * 纵向过长问题（参考 Codex 的「运行了 N 个工具 ▾」一行摘要）。
 *
 * 本模块为纯函数、无 React / UI 依赖，便于独立单元测试，也避免把聚合逻辑
 * 与编排组件（TurnTimeline）的重型依赖（xterm 等）耦合在一起。
 *
 * @module services/timeline/groupTools
 */

import type { TimelineToolItem, TurnTimelineEntry } from "@/services/timeline/projector";

/**
 * 实际渲染的条目：在投影器输出的 TurnTimelineEntry 基础上，
 * 把「长度 ≥ 2 的连续 tool 段」合并为 toolGroup，供 ToolCallGroup 收起展示。
 * 单条 tool 保持原样（不套壳），仍走 ToolCallCard / TerminalCallCard。
 */
export type RenderEntry =
  | TurnTimelineEntry
  | { kind: "toolGroup"; groupId: string; items: TimelineToolItem[] };

/**
 * 把连续 tool 段聚合成 toolGroup。
 *
 * 规则：仅相邻（中间无 assistant/thinking/status 打断）且数量 ≥ 2 的 tool 才入组；
 * 单条 tool 以及被非 tool 条目打断的段均保持原样（单条直接复用原 entry 对象，不新建外层）。
 *
 * groupId 稳定性（关键）：流式期同段工具单调 append，组从 2 增至 N 时仍属同一逻辑组。
 * 因此 groupId 仅基于「首项 callId」生成，不拼接长度——长度变化不会导致 key 变化、
 * 从而不会卸载重挂 ToolCallGroup、不会重置展开态。callId 缺失（投影层允许为空）时，
 * 回退到「首项在原始 entries 中的全局索引」：流式期条目只 append 不重排，该索引恒定，
 * 且同段内工具完成只原地更新条目、不改变索引，故 key 仍稳定（不回退到会随
 * running→completed 变化的 eventId）。
 *
 * 参数:
 *   entries - 投影器产出的按序条目。
 *
 * 返回:
 *   渲染用条目数组；未变化项的引用沿用（配合 memo 精确跳过）。
 */
export function groupConsecutiveTools(entries: TurnTimelineEntry[]): RenderEntry[] {
  const result: RenderEntry[] = [];
  let buffer: TimelineToolItem[] = [];
  let bufferStartIndex = -1;
  let firstEntry: TurnTimelineEntry | null = null;

  const flush = () => {
    if (buffer.length === 0) return;
    if (buffer.length >= 2) {
      const anchor = buffer[0].callId ?? `idx-${bufferStartIndex}`;
      // 仅用锚点（首项 callId 或稳定索引）作 key，不拼接长度，避免流式期组增长抖动。
      const groupId = `grp-${anchor}`;
      result.push({ kind: "toolGroup", groupId, items: buffer });
    } else if (firstEntry) {
      // 单条 tool：直接复用原 entry 对象，保留引用稳定以击穿 TimelineEntry memo 收益。
      result.push(firstEntry);
    }
    buffer = [];
    bufferStartIndex = -1;
    firstEntry = null;
  };

  entries.forEach((entry, index) => {
    if (entry.kind === "tool") {
      if (buffer.length === 0) {
        bufferStartIndex = index;
        firstEntry = entry;
      }
      buffer.push(entry.item);
    } else {
      flush();
      result.push(entry);
    }
  });
  flush();
  return result;
}
