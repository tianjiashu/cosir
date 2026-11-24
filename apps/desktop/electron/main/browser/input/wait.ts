import { browserError } from "../errors";
import type { BrowserOperation } from "../operation";

const POLL_MS = 250;

/**
 * 等文字出现（`text`）或消失（`textGone`），都没有就是单纯暂停。每次检查读一次与快照相同的内容（`BrowserTab.mentions`）：
 * 曾只查主文档的 `innerText`，iframe 与 Shadow DOM 里的文字快照看得见、等待却看不见，等消失会立即误报成功。
 * 代价是大页面上一次读取可达约 1 秒（实测 9000 个元素），检查间隔随之变长；落定也是这样读的。
 */
export async function waitFor(
  session: BrowserOperation,
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
    if (signal.aborted) throw signal.reason;
    // 页面被弹窗停住时读不到（`mentions` 为 null）：下一轮在这里报出，与动作循环同一条规则。
    const paused = session.paused();
    if (paused) throw paused;
    const found = await session.mentions(text);
    if (found !== null && found === wanted) return;
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
      reject(signal.reason);
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    if (signal.aborted) onAbort();
    else signal.addEventListener("abort", onAbort, { once: true });
  });
}
