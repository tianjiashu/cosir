import { HostMethodError } from "../host-link/methods";
import type { PageAction } from "./params";

/**
 * 在页面上执行一组动作（点击、输入、按键、选择、滚动、等待）。只经 CDP 合成输入；Agent 与用户输入的区分
 * 见 `BrowserTab.act`。
 *
 * 元素由快照里的 ref 指定，经 `resolveRef` 换成 `backendNodeId`；节点已不在页面上（导航、重渲染）报
 * `REF_STALE`，模型重新看快照即可。动作按序执行、遇错即停：已完成的动作不回滚，结果里说明停在哪一步，
 * 调用方随后照常返回新快照——新快照里有新的 ref，模型能直接接着做。
 */

/** `BrowserTab` 交给本模块的最小能力；不为它单独引入接口类型。 */
export type PageSession = {
  cdp<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>;
  resolveRef(ref: string): number;
  /** 动作可能触发导航：等主框架加载结束（有时限，超时不算失败）。 */
  settle(): Promise<void>;
};

export type ActResult = {
  completed: number;
  failure: { index: number; code: string; message: string } | null;
};

const MAX_WAIT_SECONDS = 15;
const POLL_MS = 250;

/** CDP 键盘事件需要的字段；`text` 只给会产生字符的键（回车提交表单靠它）。 */
const KEYS: Record<string, { code: string; keyCode: number; text?: string }> = {
  Enter: { code: "Enter", keyCode: 13, text: "\r" },
  Tab: { code: "Tab", keyCode: 9 },
  Escape: { code: "Escape", keyCode: 27 },
  Backspace: { code: "Backspace", keyCode: 8 },
  Delete: { code: "Delete", keyCode: 46 },
  ArrowUp: { code: "ArrowUp", keyCode: 38 },
  ArrowDown: { code: "ArrowDown", keyCode: 40 },
  ArrowLeft: { code: "ArrowLeft", keyCode: 37 },
  ArrowRight: { code: "ArrowRight", keyCode: 39 },
  Home: { code: "Home", keyCode: 36 },
  End: { code: "End", keyCode: 35 },
  PageUp: { code: "PageUp", keyCode: 33 },
  PageDown: { code: "PageDown", keyCode: 34 },
  Space: { code: "Space", keyCode: 32, text: " " },
};
export const SUPPORTED_KEYS = Object.keys(KEYS);

export async function runActions(session: PageSession, actions: readonly PageAction[], signal: AbortSignal): Promise<ActResult> {
  for (const [index, action] of actions.entries()) {
    if (signal.aborted) return { completed: index, failure: { index, code: "CANCELLED", message: "the call was cancelled" } };
    try {
      await run(session, action, signal);
      await session.settle();
    } catch (error) {
      const failure = describe(error);
      return { completed: index, failure: { index, ...failure } };
    }
  }
  return { completed: actions.length, failure: null };
}

async function run(session: PageSession, action: PageAction, signal: AbortSignal): Promise<void> {
  switch (action.type) {
    case "click": {
      const { x, y } = await center(session, session.resolveRef(action.ref));
      await session.cdp("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
      await session.cdp("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
      await session.cdp("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
      return;
    }
    case "type": {
      const backendNodeId = session.resolveRef(action.ref);
      await session.cdp("DOM.scrollIntoViewIfNeeded", { backendNodeId });
      await session.cdp("DOM.focus", { backendNodeId });
      if (action.clear) await callOn(session, backendNodeId, CLEAR_FIELD);
      await session.cdp("Input.insertText", { text: action.text });
      if (action.submit) await press(session, "Enter");
      return;
    }
    case "press":
      await press(session, action.key);
      return;
    case "select": {
      const problem = await callOn(session, session.resolveRef(action.ref), SELECT_OPTIONS, [action.values]);
      if (problem) throw new HostMethodError("ACTION_FAILED", problem);
      return;
    }
    case "scroll": {
      if (action.ref) {
        await session.cdp("DOM.scrollIntoViewIfNeeded", { backendNodeId: session.resolveRef(action.ref) });
        return;
      }
      // 不用 CDP 滚轮：视图不挂在窗口上时它永远不返回（实测）。
      const sign = action.direction === "down" ? 1 : -1;
      await session.cdp("Runtime.evaluate", { expression: `window.scrollBy(0, ${sign} * Math.round(innerHeight * 0.8))` });
      return;
    }
    case "wait_for":
      await waitFor(session, action.text, Math.min(action.seconds ?? 5, MAX_WAIT_SECONDS), signal);
      return;
  }
}

async function press(session: PageSession, key: string): Promise<void> {
  const spec = KEYS[key];
  const base = { key: key === "Space" ? " " : key, code: spec.code, windowsVirtualKeyCode: spec.keyCode, nativeVirtualKeyCode: spec.keyCode };
  await session.cdp("Input.dispatchKeyEvent", { type: "keyDown", ...base, ...(spec.text ? { text: spec.text } : {}) });
  await session.cdp("Input.dispatchKeyEvent", { type: "keyUp", ...base });
}

/** 先滚动到可见，再取内容框中心；没有布局（隐藏、零尺寸）报 `NOT_INTERACTABLE`。 */
async function center(session: PageSession, backendNodeId: number): Promise<{ x: number; y: number }> {
  await session.cdp("DOM.scrollIntoViewIfNeeded", { backendNodeId });
  let quad: number[];
  try {
    quad = (await session.cdp<{ model: { content: number[] } }>("DOM.getBoxModel", { backendNodeId })).model.content;
  } catch (error) {
    if (isMissingNode(error)) throw error;
    throw new HostMethodError("NOT_INTERACTABLE", "the element is not visible on the page");
  }
  return { x: (quad[0] + quad[2] + quad[4] + quad[6]) / 4, y: (quad[1] + quad[3] + quad[5] + quad[7]) / 4 };
}

/** 在元素上执行一段固定的页面函数（只用于清空输入框与选择选项，不对外开放任意脚本）。 */
async function callOn(session: PageSession, backendNodeId: number, fn: string, args: unknown[] = []): Promise<string> {
  const { object } = await session.cdp<{ object: { objectId: string } }>("DOM.resolveNode", { backendNodeId });
  const { result } = await session.cdp<{ result: { value?: unknown } }>("Runtime.callFunctionOn", {
    objectId: object.objectId,
    functionDeclaration: fn,
    arguments: args.map((value) => ({ value })),
    returnByValue: true,
  });
  return typeof result.value === "string" ? result.value : "";
}

const CLEAR_FIELD = `function () {
  if ("value" in this) { this.value = ""; this.dispatchEvent(new Event("input", { bubbles: true })); }
  else if (this.isContentEditable) { this.textContent = ""; this.dispatchEvent(new Event("input", { bubbles: true })); }
  return "";
}`;

const SELECT_OPTIONS = `function (values) {
  if (this.tagName !== "SELECT") return "the element is not a select list";
  let matched = 0;
  for (const option of this.options) {
    const hit = values.includes(option.value) || values.includes(option.label.trim());
    option.selected = hit && (this.multiple || matched === 0);
    if (hit) matched += 1;
  }
  if (matched === 0) return "no option matched " + JSON.stringify(values);
  this.dispatchEvent(new Event("input", { bubbles: true }));
  this.dispatchEvent(new Event("change", { bubbles: true }));
  return "";
}`;

async function waitFor(session: PageSession, text: string | undefined, seconds: number, signal: AbortSignal): Promise<void> {
  const deadline = Date.now() + seconds * 1000;
  if (text === undefined) {
    await sleep(seconds * 1000, signal);
    return;
  }
  while (Date.now() < deadline && !signal.aborted) {
    const { result } = await session.cdp<{ result: { value?: unknown } }>("Runtime.evaluate", {
      expression: `Boolean(document.body && document.body.innerText.includes(${JSON.stringify(text)}))`,
      returnByValue: true,
    });
    if (result.value === true) return;
    await sleep(POLL_MS, signal);
  }
  throw new HostMethodError("WAIT_TIMEOUT", `the text ${JSON.stringify(text)} did not appear within ${seconds} seconds`);
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(timer);
      resolve();
    }, { once: true });
  });
}

function isMissingNode(error: unknown): boolean {
  return error instanceof Error && /No node (found|with given id)/i.test(error.message);
}

function describe(error: unknown): { code: string; message: string } {
  if (error instanceof HostMethodError) return { code: error.code, message: error.message };
  if (isMissingNode(error)) return { code: "REF_STALE", message: "the element is no longer on the page; take a new snapshot" };
  return { code: "ACTION_FAILED", message: error instanceof Error ? error.message : String(error) };
}
