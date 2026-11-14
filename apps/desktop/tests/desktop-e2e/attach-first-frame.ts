import type { TransportState } from "../../lib/assistant/contract";

/**
 * 读真实后端此刻的 canonical snapshot：打开 Session 长订阅，取首帧（恒为完整快照）后立即断开。
 *
 * 后端没有单独的快照读取接口，前端首屏也是这样拿到状态的，所以测试与产品走同一条路。
 * 404 等非 2xx 原样抛出，交给调用方断言。
 */
export async function attachFirstFrame(base: string, sessionId: number): Promise<TransportState> {
  const controller = new AbortController();
  try {
    const response = await fetch(`${base}/sessions/${sessionId}/assistant/attach`, {
      method: "POST", headers: { Accept: "text/event-stream" }, signal: controller.signal,
    });
    if (!response.ok || !response.body) throw new Error(`attach ${sessionId} failed: ${response.status}`);
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) throw new Error(`attach ${sessionId} ended before the first frame`);
      buffer += value;
      const end = buffer.indexOf("\n\n");
      if (end < 0) continue;
      const data = buffer.slice(0, end).split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trim()).join("");
      const frame = JSON.parse(data) as { kind: string; state: TransportState };
      if (frame.kind !== "full") throw new Error(`attach ${sessionId} first frame is ${frame.kind}`);
      return frame.state;
    }
  } finally {
    controller.abort();
  }
}
