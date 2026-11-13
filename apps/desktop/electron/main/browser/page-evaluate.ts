import { HostMethodError } from "../host-link/method-registry";

/** 执行 JS 的时限（含等 Promise）：页面本身没卡住，超时是脚本的结果，不是页面无响应。 */
const EVALUATE_TIMEOUT_MS = 15_000;
/** 返回值的上限：大对象整个经链路送回再交给全局输出预算截断，截在哪里都没有意义。 */
const VALUE_MAX_CHARS = 8_000;
/** 判断主线程是否空闲的探测时限：空闲时一条最简单的求值几毫秒就回来。 */
const PROBE_TIMEOUT_MS = 500;
/** 每次求值产生的对象句柄挂在各自的组上、结束时一并释放：组名不能共用，否则并发的求值会释放掉对方的句柄。 */
let evaluations = 0;

/**
 * 在对象上执行：转成 JSON 文本；转不了（函数、Symbol 等）时返回 null，由调用方改用 CDP 给的描述。循环引用等抛出。
 * 严格模式：否则 `this` 是 Symbol 这类原始值时会被装箱成对象，转成 `{}`。
 */
const TO_JSON = "function () { 'use strict'; const text = JSON.stringify(this); return text === undefined ? null : text; }";

type RemoteObject = { type: string; subtype?: string; value?: unknown; unserializableValue?: string; description?: string; objectId?: string };
type EvaluateResponse = { result: RemoteObject; exceptionDetails?: { text: string; exception?: { description?: string; value?: unknown } } };
type Send = (method: string, params: Record<string, unknown>, options?: { signal?: AbortSignal; timeoutMs?: number }) => Promise<unknown>;

/** 值是合法的 JSON 文本（截断时截在中间、`truncated` 为真），或 `"undefined"`；脚本抛错、转不成 JSON、超时都是 `error`。 */
export type Evaluation = { value: string | null; truncated: boolean; error: string | null };

/**
 * 在页面主世界执行 JS 表达式，等待 Promise，把值转成 JSON 文本（截断）。
 *
 * 不让 CDP 按值返回（`returnByValue`）：遇到 `window`、循环引用这类对象它会以协议错误失败，变成宿主内部错误；
 * 改为拿到对象句柄后在页面里 `JSON.stringify`，转不了的如实报成脚本错误。DOM 节点、函数、`NaN`、BigInt 等不是
 * JSON 的值，返回它们的简短描述（作为 JSON 字符串）。
 *
 * 超时或调用取消时，主线程还被脚本占着（死循环、长计算）就发 `Runtime.terminateExecution` 打断它：否则之后这个
 * 标签（与同进程的标签）的命令全都回不来。只在等 Promise 时主线程是空闲的，不能发——没有脚本在跑时它会打断
 * 同一渲染进程里「下一段」执行的 JS（页面自己的回调、别的标签、Agent 下一个动作），所以先用一条最简单的求值
 * 探测：回得来就是空闲。探测分不清占着主线程的是不是这段脚本（页面自己或同进程别的标签恰好有长会话时会打断
 * 那一段），是低概率下的已知局限。转 JSON 是同步执行、不等 Promise，它超时就一定是主线程被占着，直接打断。执行期间页面跳走或重载，Chromium 以协议错误结束求值，这是脚本的结果，不是宿主故障。
 *
 * 用 `Runtime.evaluate` 不开 `Runtime.enable`（见 `BrowserTab` 的说明）；不给用户激活，脚本不能借它绕过弹出窗口
 * 的拦截。页面停住（弹窗）时 `send` 以 `DIALOG_OPEN` 失败，由调用方判断是不是脚本自己弹的。
 */
export async function evaluateExpression(send: Send, expression: string, signal: AbortSignal): Promise<Evaluation> {
  const objectGroup = `cosir-evaluate-${++evaluations}`;
  try {
    let response: EvaluateResponse;
    try {
      response = await send("Runtime.evaluate", {
        expression, awaitPromise: true, returnByValue: false, userGesture: false, objectGroup,
      }, { signal, timeoutMs: EVALUATE_TIMEOUT_MS }) as EvaluateResponse;
    } catch (error) {
      // 只有求值本身的结局才是脚本的结果；之后转 JSON 的命令失败照常上抛。
      return await interrupted(send, error, signal);
    }
    if (response.exceptionDetails) return failed(describeException(response.exceptionDetails));
    return await serialize(send, response.result, signal);
  } finally {
    // 释放失败（页面已停住或已导航）不影响结果：句柄随页面一起回收。
    send("Runtime.releaseObjectGroup", { objectGroup }).catch(() => undefined);
  }
}

/** 求值没有正常返回：超时与取消时打断仍在跑的脚本；页面跳走是脚本的结果；弹窗（`DIALOG_OPEN`）等宿主错误照常上抛。 */
async function interrupted(send: Send, error: unknown, signal: AbortSignal): Promise<Evaluation> {
  const timedOut = error instanceof HostMethodError && error.code === "PAGE_UNRESPONSIVE";
  const stopped = (timedOut || signal.aborted) && await stopIfRunning(send);
  if (timedOut) {
    return failed(stopped
      ? `the expression was still running after ${EVALUATE_TIMEOUT_MS / 1000} seconds and was stopped (a long-running loop or computation)`
      : `the expression did not finish within ${EVALUATE_TIMEOUT_MS / 1000} seconds (it is waiting on a Promise that has not settled)`);
  }
  if (error instanceof HostMethodError || signal.aborted) throw error;
  return failed(`the expression did not finish: ${error instanceof Error ? error.message : String(error)} (the page navigated or reloaded meanwhile)`);
}

/** 主线程还被占着就打断它，返回是否打断成功；探测回得来（空闲、只在等 Promise）就什么也不做。 */
async function stopIfRunning(send: Send): Promise<boolean> {
  const busy = await send("Runtime.evaluate", { expression: "0" }, { timeoutMs: PROBE_TIMEOUT_MS }).then(
    () => false,
    (error: unknown) => error instanceof HostMethodError && error.code === "PAGE_UNRESPONSIVE",
  );
  return busy && terminate(send);
}

/** 打断正在执行的 JS，返回命令是否成功（失败时如实不说「已打断」）。 */
function terminate(send: Send): Promise<boolean> {
  return send("Runtime.terminateExecution", {}).then(() => true, () => false);
}

async function serialize(send: Send, result: RemoteObject, signal: AbortSignal): Promise<Evaluation> {
  if (result.type === "undefined") return { value: "undefined", truncated: false, error: null };
  if (result.objectId === undefined || result.subtype === "node") {
    // 原始值按值回来；不是 JSON 的（NaN、BigInt、Symbol）与 DOM 节点用描述。
    const text = result.value !== undefined && result.subtype !== "node"
      ? JSON.stringify(result.value)
      : JSON.stringify(result.unserializableValue ?? result.description ?? result.type);
    return clipped(text);
  }
  let converted: EvaluateResponse;
  try {
    converted = await send("Runtime.callFunctionOn", {
      objectId: result.objectId, functionDeclaration: TO_JSON, returnByValue: true,
    }, { signal }) as EvaluateResponse;
  } catch (error) {
    // 死循环的 getter、极大的对象：同步执行超时或调用取消，主线程一定还被占着。
    const timedOut = error instanceof HostMethodError && error.code === "PAGE_UNRESPONSIVE";
    if (!timedOut && !signal.aborted) throw error;
    const stopped = await terminate(send);
    if (!timedOut) throw error;
    return failed(`turning the value into JSON did not finish${stopped ? " and was stopped" : ""} (a getter that never returns, or a value too large)`);
  }
  if (converted.exceptionDetails) return failed(`the value could not be turned into JSON: ${describeException(converted.exceptionDetails)}`);
  const text = converted.result.value;
  return clipped(typeof text === "string" ? text : JSON.stringify(result.description ?? result.type));
}

/** 抛出的是 Error 时用它的描述（含堆栈）；抛出原始值（如 `Promise.reject("nope")`）时没有描述，补上值本身。 */
function describeException({ text, exception }: NonNullable<EvaluateResponse["exceptionDetails"]>): string {
  const thrown = exception?.description ?? (exception?.value === undefined ? text : `${text} ${JSON.stringify(exception.value)}`);
  return thrown.slice(0, VALUE_MAX_CHARS);
}

function clipped(text: string): Evaluation {
  return text.length > VALUE_MAX_CHARS
    ? { value: `${text.slice(0, VALUE_MAX_CHARS)}…`, truncated: true, error: null }
    : { value: text, truncated: false, error: null };
}

function failed(error: string): Evaluation {
  return { value: null, truncated: false, error };
}
