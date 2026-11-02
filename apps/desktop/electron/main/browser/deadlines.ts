import deadlines from "./contract/method-deadlines.json";
import { abortable } from "./abortable";
import { browserError } from "./errors";

/**
 * 浏览器方法的总时限：宿主保证每次调用在这之内答复（超时也是一种答复，码为 `DEADLINE_EXCEEDED`）。时限的唯一来源是
 * `contract/method-deadlines.json`（秒）；后端的等待时限 = 它 + 链路余量，镜像在后端 `host_request.py`，由
 * `test_browser_contract_guard.py` 核对。这样后端不必再按宿主内部各步的超时去推算「最坏要多久」——那份推算
 * 随宿主实现变化而漂移，漂到比宿主实际耗时短时，后端先超时、撤销调用，模型拿不到做到了哪一步。
 *
 * 在方法注册处（`methods.ts`）一处执行，覆盖等标签可用、新开标签与结果读取；需要「到点前停下并交出部分结果」的方法
 * （act）在里面另设更早的截止（`budgetSignal`）。
 */
export type BrowserMethod = keyof typeof deadlines;

export function deadlineMs(method: BrowserMethod): number {
  return deadlines[method] * 1000;
}

/** 在时限内执行；到点时 `run` 收到的信号中止，调用以 `DEADLINE_EXCEEDED` 结束（不等 `run` 自己收尾）。 */
export async function withinDeadline<T>(method: BrowserMethod, signal: AbortSignal, run: (signal: AbortSignal) => Promise<T>): Promise<T> {
  const expired = expiring(deadlineMs(method), `${method} did not finish within ${deadlines[method]} seconds`);
  const scoped = AbortSignal.any([signal, expired.signal]);
  try {
    return await abortable(run(scoped), scoped);
  } catch (error) {
    // 取消先到时照旧按取消答复；到点之后 `run` 抛出的任何错误都是被时限打断的后果。
    if (expired.signal.aborted && !signal.aborted) throw expired.signal.reason;
    throw error;
  } finally {
    expired.clear();
  }
}

/**
 * 时限到前 `reserveMs` 中止的信号，中止原因是 `DEADLINE_EXCEEDED`：给分步执行的方法留出读结果的时间，到点停在
 * 当前一步，照常交出已完成的部分。计时不随调用结束清除（到点时中止一个已无人监听的信号，无副作用）。
 */
export function budgetSignal(method: BrowserMethod, reserveMs: number, message: string): AbortSignal {
  return expiring(deadlineMs(method) - reserveMs, message).signal;
}

function expiring(ms: number, message: string): { signal: AbortSignal; clear: () => void } {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(browserError("DEADLINE_EXCEEDED", message)), ms);
  timer.unref();
  return { signal: controller.signal, clear: () => clearTimeout(timer) };
}
