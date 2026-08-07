/**
 * SSE 帧解析纯函数。
 *
 * 把单条 SSE 帧（``event:`` + ``data:`` 格式的原始文本）拆解为事件类型与
 * data 原始字符串。只做「行级拆分」，不做 JSON 解析——因为不同事件类型的
 * data 结构不同（如 turn 级 ``RuntimeEvent`` 与 workspace 状态事件
 * ``WorkspaceEvent``），由调用方决定如何 parse 成自己的类型。
 *
 * 单一职责：SSE 帧文本 → 事件类型 + data 字符串。供 ``sse.ts``（turn 级流）与
 * ``api.ts`` 的 ``connectWorkspaceEventStream``（workspace 状态事件流）共用，
 * 避免两处手写重复的逐行解析。
 *
 * 遵循 W3C SSE 规范（https://html.spec.whatwg.org/multipage/server-sent-events.html）：
 * - 同一帧内可出现多条 ``data:`` 行，须按 ``\n`` 连接成完整 data 字段（而非后者覆盖前者）。
 *   后端若把含换行的长 JSON（如工具结果 ``content``）拆成多条 data 行，将命中此路径。
 * - ``data:`` 为空串（含 ``data:`` 后无内容）是合法帧，对应心跳 / ping，不得整帧丢弃。
 *   仅当帧完全缺失 ``event:`` 或完全缺失 ``data:`` 行时才返回 null。
 *
 * @module services/sseParser
 */

/** 单条 SSE 帧的拆分结果。 */
export interface ParsedSSEFrame {
  /** ``event:`` 行的值（去空白）。 */
  eventType: string;
  /** ``data:`` 行的值（去空白，未做 JSON 解析）；多行按 ``\n`` 拼接。 */
  data: string;
}

/**
 * 解析单条 SSE 帧文本。
 *
 * 按 W3C SSE 规范处理：多条 ``data:`` 行以换行拼接；空 data 视为合法帧。
 *
 * @param text - 单条 SSE 帧原始文本（含 ``event:`` 与 ``data:`` 行）。
 * @returns 解析成功返回 ``{ eventType, data }``；缺 ``event:`` 或完全缺 ``data:`` 行时返回 null。
 *
 * @sideeffect 无。
 */
export function parseSSEFrame(text: string): ParsedSSEFrame | null {
  let eventType = "";
  let hasDataLine = false;
  const dataLines: string[] = [];
  for (const line of text.split("\n")) {
    if (line.startsWith("event:")) {
      eventType = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      // 规范要求只剥离一个前导空格；其余前导空白与全部尾随空白保留，
      // 避免误删 payload 中作为有效内容的空白。空串（"data:" 后无内容）合法。
      hasDataLine = true;
      dataLines.push(line.slice(5).replace(/^ /, ""));
    }
  }
  // 仅当完全缺失 event 或完全缺失 data 行时判定为非法帧；空 data 字符串合法。
  if (!eventType || !hasDataLine) {
    return null;
  }
  return { eventType, data: dataLines.join("\n") };
}
