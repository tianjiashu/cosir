import { browserError } from "../errors";
import { stopped, type PageSession } from "./session";

const POLL_MS = 250;

/** 等文字出现（`text`）或消失（`textGone`），都没有就是单纯暂停。按 `innerText` 判断，隐藏的元素不算在页面上。 */
export async function waitFor(
  session: PageSession,
  target: { text?: string; textGone?: string },
  seconds: number,
  signal: AbortSignal,
): Promise<void> {
  const deadline = Date.now() + seconds * 1000;
  const text = target.text ?? target.textGone;
  if (text === undefined) {
    await sleep(seconds * 1000, signal);
    return;
  }
  const wanted = target.text !== undefined;
  while (Date.now() < deadline) {
    if (signal.aborted) throw stopped(signal);
    const { result } = await session.cdp<{ result: { value?: unknown } }>("Runtime.evaluate", {
      expression: `Boolean(document.body && document.body.innerText.includes(${JSON.stringify(text)}))`,
      returnByValue: true,
    });
    if (result.value === wanted) return;
    await sleep(POLL_MS, signal);
  }
  const outcome = wanted ? `did not appear within ${seconds} seconds` : `is still on the page after ${seconds} seconds`;
  throw browserError("WAIT_TIMEOUT", `the text ${JSON.stringify(text)} ${outcome}`);
}

/** 暂停 `ms`；调用取消或标签关闭时以 `stopped` 的原因提前结束（不当成等待超时）。 */
function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      clearTimeout(timer);
      reject(stopped(signal));
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    if (signal.aborted) onAbort();
    else signal.addEventListener("abort", onAbort, { once: true });
  });
}
