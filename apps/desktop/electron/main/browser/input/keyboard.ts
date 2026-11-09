import type { Modifier } from "../params";
import type { PageNode } from "../ref-registry";
import { actionable, element, mutateOn, onNode } from "./element";
import { modifierBits } from "./modifiers";
import { CLEAR_FIELD } from "./page-functions";
import type { BrowserOperation } from "../operation";

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

/** 契约（后端参数模型导出的 schema）里的按键都要在这里有对应：单测按 schema 逐个核对。 */
export function isSupportedKey(key: string): boolean {
  return Object.hasOwn(KEYS, key) || CHARACTER_KEY.test(key);
}

/**
 * 聚焦字段（可先清空）后写入文字，可再按回车提交。默认一次写入（`insertText`：快，只触发 input 事件）；`keyByKey`
 * 时每个字符一次按键（keydown / keypress / input / keyup 照真实打字触发），给按键盘事件工作的控件（自动补全、
 * 掩码输入、验证码格子）。换行按回车键处理。
 */
export async function typeText(
  session: BrowserOperation,
  ref: string,
  text: string,
  options: { clear: boolean; submit: boolean; keyByKey: boolean },
): Promise<void> {
  const node = await element(session, ref);
  await actionable(session, node, true);
  await focus(session, node);
  if (options.clear) await mutateOn(session, node, CLEAR_FIELD);
  if (options.keyByKey) {
    for (const char of text) await stroke(session, charSpec(char));
  } else {
    await session.effect(() => session.cdp("Input.insertText", { text }));
  }
  if (options.submit) await stroke(session, keySpec("Enter", false));
}

/** 按一个键（可带修饰键）；给了 ref 时先聚焦它，否则按在当前焦点上。 */
export async function pressKey(session: BrowserOperation, key: string, modifiers: readonly Modifier[], ref: string | undefined): Promise<void> {
  if (ref) {
    const node = await element(session, ref);
    await actionable(session, node, false, false);
    await focus(session, node);
  }
  const spec = keySpec(key, modifiers.includes("Shift"));
  // 按住 Alt / Control / Meta 时是快捷键，不产生字符；只按 Shift 时照常产生（大写）字符。
  const shortcut = modifiers.some((modifier) => modifier !== "Shift");
  await stroke(session, shortcut ? { ...spec, text: undefined } : spec, modifierBits(modifiers));
}

/** 聚焦发到节点所在的会话；之后的按键与输入发主会话，落在有焦点的框架里（跨进程子框架也一样，实测）。 */
async function focus(session: BrowserOperation, node: PageNode): Promise<void> {
  await onNode(session, node, "DOM.scrollIntoViewIfNeeded");
  await onNode(session, node, "DOM.focus");
}

type KeySpec = { key: string; code?: string; keyCode?: number; text?: string };

/** 一次按键：keyDown（带字符时产生输入）与 keyUp。 */
async function stroke(session: BrowserOperation, spec: KeySpec, modifiers = 0): Promise<void> {
  const base = {
    key: spec.key,
    modifiers,
    ...(spec.code ? { code: spec.code } : {}),
    ...(spec.keyCode ? { windowsVirtualKeyCode: spec.keyCode, nativeVirtualKeyCode: spec.keyCode } : {}),
  };
  await session.inputUnit("Input.dispatchKeyEvent",
    { type: "keyDown", ...base, ...(spec.text ? { text: spec.text } : {}) },
    { type: "keyUp", ...base });
}

/** 逐键输入里的一个字符：键盘上有的键（字母、数字、空格、换行）带键位，其余（标点、中文等）只带字符。 */
function charSpec(char: string): KeySpec {
  if (char === "\n") return keySpec("Enter", false);
  if (char === " ") return keySpec("Space", false);
  const lower = char.toLowerCase();
  if (CHARACTER_KEY.test(lower)) return keySpec(lower, lower !== char);
  return { key: char, text: char };
}

function keySpec(key: string, shift: boolean): KeySpec {
  const named = KEYS[key];
  if (named) return { key: key === "Space" ? " " : key, ...named };
  const char = shift ? key.toUpperCase() : key;
  const digit = /\d/.test(key);
  return { key: char, code: digit ? `Digit${key}` : `Key${key.toUpperCase()}`, keyCode: key.toUpperCase().charCodeAt(0), text: char };
}
