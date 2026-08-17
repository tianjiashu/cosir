/**
 * SSE 帧流式分发器。
 *
 * 基于 Vercel 维护的成熟库 `eventsource-parser`（OpenAI 官方 SDK 同款）实现
 * W3C SSE 帧解析，替代原手写逐行解析 `parseSSEFrame`。只做「帧 → 事件类型 +
 * data 字符串」的分发，不做 JSON 解析——因为不同事件类型的 data 结构不同
 * （如 turn 级 ``RuntimeEvent`` 与 workspace 状态事件 ``WorkspaceEvent``），
 * 由调用方决定如何 parse 成自己的类型。
 *
 * 与旧实现相比的核心改进：`eventsource-parser` 原生处理流分片边界（feed 可
 * 喂入任意长度的不完整分片，内部缓冲拼接），不再依赖 ``split("\n\n")`` 手动
 * 切帧与残留 buffer 管理。
 *
 * 单一职责：SSE 帧文本流 → 事件类型 + data 字符串回调。供 ``sse.ts``（turn
 * 级流）、``api.ts`` 的 ``connectWorkspaceEventStream``（workspace 状态事件流）
 * 与 ``delegationStream.ts``（child turn 订阅流）共用。
 *
 * 行为对齐旧实现（W3C SSE 规范 https://html.spec.whatwg.org/multipage/server-sent-events.html）：
 * - 有 ``event:`` 名 + 至少一条 ``data:`` 行 → 分发 ``{ eventType, data }``；
 * - ``data:`` 为空串（含 ``data:`` 无内容）是合法帧，对应心跳 / ping，不得丢弃；
 * - 多条 ``data:`` 行按 ``\n`` 拼接（库原生支持）；
 * - 缺 ``event:`` 名 → 不分发（库对无 event 名的帧也会 dispatch，本工厂内过滤）；
 * - 缺 ``data:`` 行（只有 event 行）→ 不分发（库原生只对 dataLines > 0 的帧 dispatch）；
 * - 注释行（``:`` 开头）→ 忽略（库默认行为）；
 * - 流分片边界 → 库原生缓冲拼接；
 * - 库内无法识别为合法 SSE 字段的行/字段按规范静默丢弃，与旧实现只识别
 *   ``event:``/``data:`` 的行为一致。
 *
 * @module services/sseParser
 */

import { createParser } from "eventsource-parser";
import type { EventSourceMessage } from "eventsource-parser";

/** 单条 SSE 帧的拆分结果。 */
export interface ParsedSSEFrame {
  /** ``event:`` 行的值（去空白，与旧实现 trim 行为一致）。 */
  eventType: string;
  /** ``data:`` 行的值（未做 JSON 解析）；多行按 ``\n`` 拼接，空串为合法心跳帧。 */
  data: string;
}

/** 流式 SSE 帧解析器接口。 */
export interface SSESingleFrameParser {
  /**
   * 喂入解码后的文本分片。
   *
   * @param chunk - 任意长度的文本分片，可跨帧边界或只含半行；内部缓冲拼接，
   *   完整帧解析出来后同步调用构造时传入的 onFrame 回调。
   *
   * @sideeffect 每解析出一条完整帧即调用 onFrame 回调。
   */
  feed(chunk: string): void;

  /**
   * 重置内部状态，供流结束后复用同一实例。
   *
   * @sideeffect 清空内部缓冲与待分发字段；不消费（consume）缓冲区内的不完整残片，
   *   调用方应在 EOF 前用 ``feed("\n\n")`` 促使已完整帧被分发。
   */
  reset(): void;
}

/**
 * 创建流式 SSE 帧解析器。
 *
 * @param onFrame - 每解析出一条完整帧（有 event 名且至少一条 data 行）时同步调用；
 *   参数为 ``{ eventType, data }``，data 未做 JSON 解析。
 * @returns 实现了 ``SSESingleFrameParser`` 接口的解析器实例。
 * @throws 不主动抛出；库内无法识别字段按规范静默忽略。
 *
 * @sideeffect 构造时创建底层 eventsource-parser 实例；feed 命中完整帧时同步触发 onFrame。
 */
export function createSSEFrameParser(onFrame: (frame: ParsedSSEFrame) => void): SSESingleFrameParser {
  const parser = createParser({
    onEvent: (event: EventSourceMessage) => {
      // eventsource-parser 对无 `event:` 名的帧（仅 data）也会 dispatch（event 为
      // 空串/undefined），而旧 parseSSEFrame 对缺 event 名的帧返回 null。此处过滤，
      // 保持旧行为：缺 event 名 → 不分发。
      if (!event.event) {
        return;
      }
      // 库对 `event:` 值只剥离一个前导空格、不 trim 尾随空白；旧实现做了完整 trim，
      // 这里补 trim 保持事件类型归一化行为一致。
      onFrame({ eventType: event.event.trim(), data: event.data });
    },
  });

  return {
    feed: (chunk: string) => {
      parser.feed(chunk);
    },
    reset: () => {
      parser.reset();
    },
  };
}
