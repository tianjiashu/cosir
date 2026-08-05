/**
 * SSE 帧解析纯函数。
 *
 * 把单条 SSE 帧（``event:`` + ``data:`` 格式的原始文本）拆解为事件类型与
 * data 原始字符串。只做「行级拆分」，不做 JSON 解析——因为不同事件类型的
 * data 结构不同（如 turn 级 ``RuntimeEvent`` 与 workspace 索引进度
 * ``WorkspaceIndexEvent``），由调用方决定如何 parse 成自己的类型。
 *
 * 单一职责：SSE 帧文本 → 事件类型 + data 字符串。供 ``sse.ts``（turn 级流）与
 * ``api.ts`` 的 ``connectWorkspaceIndexStream``（workspace 索引进度流）共用，
 * 避免两处手写重复的逐行解析。
 *
 * @module services/sseParser
 */

/** 单条 SSE 帧的拆分结果。 */
export interface ParsedSSEFrame {
  /** ``event:`` 行的值（去空白）。 */
  eventType: string;
  /** ``data:`` 行的值（去空白，未做 JSON 解析）。 */
  data: string;
}

/**
 * 解析单条 SSE 帧文本。
 *
 * @param text - 单条 SSE 帧原始文本（含 ``event:`` 与 ``data:`` 行）。
 * @returns 解析成功返回 ``{ eventType, data }``；缺 event 或 data 时返回 null。
 */
export function parseSSEFrame(text: string): ParsedSSEFrame | null {
  let eventType = "";
  let dataStr = "";
  for (const line of text.split("\n")) {
    if (line.startsWith("event:")) {
      eventType = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataStr = line.slice(5).trim();
    }
  }
  if (!eventType || !dataStr) {
    return null;
  }
  return { eventType, data: dataStr };
}
