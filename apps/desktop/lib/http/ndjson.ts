/**
 * 逐行读取 JSON 行流（`application/x-ndjson`）直到流结束；`decode` 在交给调用方前校验每一行。
 *
 * 结束（含异常）时取消并释放 reader：调用方中止请求后不会留下挂起的读取。
 */
export async function readNdjson<T>(
  response: Response,
  onValue: (value: T) => void,
  decode: (value: unknown) => T = (value) => value as T,
): Promise<void> {
  if (!response.body) throw new Error("服务未返回数据流");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let pending = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      pending += decoder.decode(value, { stream: !done });
      const lines = pending.split("\n");
      pending = lines.pop() ?? "";
      for (const line of lines)
        if (line.trim()) onValue(decode(JSON.parse(line)));
      if (done) break;
    }
    if (pending.trim()) onValue(decode(JSON.parse(pending)));
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
