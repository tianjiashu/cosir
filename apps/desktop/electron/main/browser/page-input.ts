import { HostMethodError } from "../host-link/methods";
import type { BrowserDialog, BrowserDialogAnswer } from "../../shared/host-contract";
import type { PageAction } from "./params";

/**
 * 在页面上执行一组动作（点击、悬停、输入、按键、选择、滚动、等待、回答弹窗）。只经 CDP 合成输入；Agent 与用户输入的
 * 区分见 `BrowserTab.act`。
 *
 * 元素由快照里的 ref 指定：ref 按节点稳定、可能来自更早的快照，所以每次动作前都向页面确认节点还在文档里
 * （`element`）——已删除或已脱离文档的节点一律报 `REF_STALE`，不让焦点落空后把字输进别的元素。
 * 动作按序执行、遇错即停：已完成的动作不回滚，结果里说明停在哪一步，调用方随后照常返回新快照，模型能直接
 * 接着做。
 *
 * 弹窗：页面停住时（本标签或同进程别的标签在弹窗）CDP 命令回不来，以 `DIALOG_OPEN` 失败。只有动作在它的
 * 「生效命令」（`effect`：点击的按下与松开、悬停的移动、输入与按键、选择、滚动）上遇到它，才说明是动作自己触发
 * 的弹窗、动作已生效，按完成处理并记下这个弹窗；其余位置（定位元素、`wait_for` 轮询……）遇到它就是失败——页面
 * 自己在别的时机弹了窗，动作并没有发生。`dialog` 动作只回答指明 id 的弹窗，或本批前面的动作弹出的那个，所以
 * 「点删除 + 确认」能在同一批里完成，又不会把确认答给一个没看见的弹窗。
 */

/** `BrowserTab` 交给本模块的最小能力；不为它单独引入接口类型。 */
export type PageSession = {
  cdp<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>;
  resolveRef(ref: string): number;
  /** 动作可能触发导航：等主框架加载结束（有时限，超时不算失败）。 */
  settle(): Promise<void>;
  /** CSS 像素 → CDP 输入坐标（视图坐标）：面板缩放显示视口时两者不同。 */
  toInput(point: { x: number; y: number }): { x: number; y: number };
  /** 页面停着的原因（本标签或同进程别的标签有待答弹窗）；没停时为 null。 */
  paused(): HostMethodError | null;
  /** 本标签待答的弹窗。 */
  dialog(): BrowserDialog | null;
  answerDialog(id: number, answer: BrowserDialogAnswer): void;
};

/** 动作的生效命令遇到弹窗：动作已生效，弹窗是它触发的。 */
class DialogOpenedByAction extends Error {}

/** 本批执行中的共享状态：前面的动作弹出的弹窗（`dialog` 动作省略 id 时只能回答它）。 */
type Batch = { openedDialog: number | null };

export type ActResult = {
  completed: number;
  failure: { index: number; code: string; message: string } | null;
};

const MAX_WAIT_SECONDS = 15;
const POLL_MS = 250;

/** CDP 键盘事件需要的字段；`text` 只给会产生字符的键（回车提交表单靠它）。单个字母与数字由 `keySpec` 现算。 */
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
const CHARACTER_KEY = /^[a-z0-9]$/;

export function isSupportedKey(key: string): boolean {
  return Object.hasOwn(KEYS, key) || CHARACTER_KEY.test(key);
}

/**
 * 修饰键。`ControlOrMeta` 在 macOS 上是 Meta、其他平台是 Control（与 Playwright 同名约定），模型不必知道
 * 宿主平台。只保证页面自己处理的快捷键（如 Cmd+Enter 发送）：macOS 上 CDP 合成的编辑快捷键（全选、撤销等）
 * 还需另传编辑命令才会生效，这里不提供，编辑文字用 `type`。
 */
export const MODIFIERS = ["Alt", "Control", "Meta", "Shift", "ControlOrMeta"] as const;
export type Modifier = (typeof MODIFIERS)[number];

/** CDP `Input.dispatchKeyEvent` 的修饰键位。 */
const MODIFIER_BITS = { Alt: 1, Control: 2, Meta: 4, Shift: 8 } as const;

export async function runActions(session: PageSession, actions: readonly PageAction[], signal: AbortSignal): Promise<ActResult> {
  const batch: Batch = { openedDialog: null };
  for (const [index, action] of actions.entries()) {
    if (signal.aborted) return { completed: index, failure: { index, code: "CANCELLED", message: "the call was cancelled" } };
    const paused = session.paused();
    if (paused && action.type !== "dialog") return { completed: index, failure: { index, code: paused.code, message: paused.message } };
    try {
      await run(session, action, signal, batch);
      await session.settle();
    } catch (error) {
      if (error instanceof DialogOpenedByAction) {
        batch.openedDialog = session.dialog()?.id ?? null;
        continue;
      }
      return { completed: index, failure: { index, ...describe(error) } };
    }
  }
  return { completed: actions.length, failure: null };
}

/** 动作的生效命令：它遇到弹窗说明弹窗是这个动作触发的（见模块说明）。 */
async function effect<T>(command: Promise<T>): Promise<T> {
  try {
    return await command;
  } catch (error) {
    if (error instanceof HostMethodError && error.code === "DIALOG_OPEN") throw new DialogOpenedByAction();
    throw error;
  }
}

async function run(session: PageSession, action: PageAction, signal: AbortSignal, batch: Batch): Promise<void> {
  switch (action.type) {
    case "click": {
      const { x, y } = session.toInput(await center(session, await element(session, action.ref)));
      await session.cdp("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
      await effect(session.cdp("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 }));
      await effect(session.cdp("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 }));
      return;
    }
    case "hover": {
      // 悬停状态留到下一次鼠标移动；之后的 click 会移走指针，靠延迟收起的菜单一般能容忍。
      const { x, y } = session.toInput(await center(session, await element(session, action.ref)));
      await effect(session.cdp("Input.dispatchMouseEvent", { type: "mouseMoved", x, y }));
      return;
    }
    case "type": {
      const backendNodeId = await element(session, action.ref);
      await session.cdp("DOM.scrollIntoViewIfNeeded", { backendNodeId });
      await session.cdp("DOM.focus", { backendNodeId });
      if (action.clear) await callOn(session, backendNodeId, CLEAR_FIELD);
      await effect(session.cdp("Input.insertText", { text: action.text }));
      if (action.submit) await press(session, "Enter");
      return;
    }
    case "press":
      if (action.ref) {
        const backendNodeId = await element(session, action.ref);
        await session.cdp("DOM.scrollIntoViewIfNeeded", { backendNodeId });
        await session.cdp("DOM.focus", { backendNodeId });
      }
      await press(session, action.key, action.modifiers);
      return;
    case "select": {
      const problem = await effect(callOn(session, await element(session, action.ref), SELECT_OPTIONS, [action.values]));
      if (problem) throw new HostMethodError("ACTION_FAILED", problem);
      return;
    }
    case "scroll":
      await scroll(session, action.direction, action.ref);
      return;
    case "wait_for":
      await waitFor(session, action, Math.min(action.seconds ?? 5, MAX_WAIT_SECONDS), signal);
      return;
    case "dialog": {
      const current = session.dialog();
      if (!current) throw new HostMethodError("NO_DIALOG", "this tab is not showing a dialog");
      const expected = action.id ?? batch.openedDialog;
      if (expected !== current.id) {
        const asked = action.id === undefined ? "no earlier action in this call opened it" : `not #${action.id}`;
        throw new HostMethodError("DIALOG_MISMATCH", `the tab is showing dialog #${current.id}; ${asked}`);
      }
      session.answerDialog(current.id, { accept: action.accept, text: action.text });
      return;
    }
  }
}

/** ref → 节点，并确认它还在文档里：删除的节点 CDP 报找不到，脱离文档但未回收的节点要问页面。 */
async function element(session: PageSession, ref: string): Promise<number> {
  const backendNodeId = session.resolveRef(ref);
  if (await callOn(session, backendNodeId, IS_CONNECTED)) {
    throw new HostMethodError("REF_STALE", `the element ${ref} is no longer on the page; use a ref from the latest snapshot`);
  }
  return backendNodeId;
}

async function press(session: PageSession, key: string, modifiers: readonly Modifier[] = []): Promise<void> {
  const bits = modifiers.reduce((sum, item) => sum | MODIFIER_BITS[resolveModifier(item)], 0);
  const spec = keySpec(key, modifiers.includes("Shift"));
  // 按住 Alt / Control / Meta 时是快捷键，不产生字符；只按 Shift 时照常产生（大写）字符。
  const text = bits & ~MODIFIER_BITS.Shift ? undefined : spec.text;
  const base = { key: spec.key, code: spec.code, windowsVirtualKeyCode: spec.keyCode, nativeVirtualKeyCode: spec.keyCode, modifiers: bits };
  await effect(session.cdp("Input.dispatchKeyEvent", { type: "keyDown", ...base, ...(text ? { text } : {}) }));
  await effect(session.cdp("Input.dispatchKeyEvent", { type: "keyUp", ...base }));
}

function resolveModifier(modifier: Modifier): keyof typeof MODIFIER_BITS {
  if (modifier !== "ControlOrMeta") return modifier;
  return process.platform === "darwin" ? "Meta" : "Control";
}

function keySpec(key: string, shift: boolean): { key: string; code: string; keyCode: number; text?: string } {
  const named = KEYS[key];
  if (named) return { key: key === "Space" ? " " : key, ...named };
  const char = shift ? key.toUpperCase() : key;
  const digit = /\d/.test(key);
  return { key: char, code: digit ? `Digit${key}` : `Key${key.toUpperCase()}`, keyCode: key.toUpperCase().charCodeAt(0), text: char };
}

/**
 * 有方向时滚动一屏左右：带 ref 滚动包含该元素的可滚动区域（虚拟列表只渲染可见部分，只有滚它才有后面的
 * 内容），不带 ref 滚动整页；滚不动报 `SCROLL_END`，让无限滚动的批次停下而不是空转。只有 ref 时把元素滚入
 * 视野（截图前定位用；点击与输入会自己滚）。不用 CDP 滚轮：视图不挂在窗口上时它永远不返回（实测）。
 */
async function scroll(session: PageSession, direction: "up" | "down" | undefined, ref: string | undefined): Promise<void> {
  if (direction === undefined) {
    await session.cdp("DOM.scrollIntoViewIfNeeded", { backendNodeId: await element(session, ref!) });
    return;
  }
  const down = direction === "down";
  const problem = ref
    ? await effect(callOn(session, await element(session, ref), SCROLL_AREA, [down]))
    : await effect(evaluate(session, `(${SCROLL_AREA}).call(document.documentElement, ${down})`));
  if (problem) throw new HostMethodError("SCROLL_END", problem);
}

/** 先滚动到可见，再取内容框中心（CSS 像素）；没有布局（隐藏、零尺寸）报 `NOT_INTERACTABLE`。 */
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

/** 在元素上执行一段本模块固定的页面函数（不对外开放任意脚本）；约定返回空串表示成功，否则是问题说明。 */
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

async function evaluate(session: PageSession, expression: string): Promise<string> {
  const { result } = await session.cdp<{ result: { value?: unknown } }>("Runtime.evaluate", { expression, returnByValue: true });
  return typeof result.value === "string" ? result.value : "";
}

const IS_CONNECTED = `function () { return this.isConnected ? "" : "detached"; }`;

/**
 * 从元素往上找最近的可滚动区域（含自身），找不到就滚整页。`behavior: "instant"` 绕过页面的平滑滚动：
 * 否则滚动异步进行，立刻比较位置会误判为滚不动。
 */
const SCROLL_AREA = `function (down) {
  const page = document.scrollingElement || document.documentElement;
  const scrollable = (node) => /(auto|scroll|overlay)/.test(getComputedStyle(node).overflowY) && node.scrollHeight > node.clientHeight;
  let target = this;
  while (target && target !== document.body && target !== document.documentElement && !scrollable(target)) target = target.parentElement;
  if (!target || target === document.body || target === document.documentElement) target = page;
  const before = target.scrollTop;
  const height = target === page ? innerHeight : target.clientHeight;
  target.scrollBy({ top: (down ? 1 : -1) * Math.round(height * 0.8), behavior: "instant" });
  if (target.scrollTop !== before) return "";
  return (target === page ? "the page" : "this area") + " is already at the " + (down ? "bottom" : "top");
}`;

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

/** 等文字出现（`text`）或消失（`textGone`），都没有就是单纯暂停。按 `innerText` 判断，隐藏的元素不算在页面上。 */
async function waitFor(
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
  while (Date.now() < deadline && !signal.aborted) {
    const { result } = await session.cdp<{ result: { value?: unknown } }>("Runtime.evaluate", {
      expression: `Boolean(document.body && document.body.innerText.includes(${JSON.stringify(text)}))`,
      returnByValue: true,
    });
    if (result.value === wanted) return;
    await sleep(POLL_MS, signal);
  }
  const outcome = wanted ? `did not appear within ${seconds} seconds` : `is still on the page after ${seconds} seconds`;
  throw new HostMethodError("WAIT_TIMEOUT", `the text ${JSON.stringify(text)} ${outcome}`);
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
