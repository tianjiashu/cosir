import { browserError } from "../errors";
import { actionable, callOn, element, evaluate, onNode } from "./element";
import type { BrowserOperation } from "../operation";

/**
 * 在页面里执行的固定函数（滚动、清空字段、选择下拉项）与用它们实现的动作。函数体是本模块里写死的字符串，
 * 不对外开放任意脚本；约定返回空串表示成功，否则是问题说明。
 */

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

export const CLEAR_FIELD = `function () {
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

/**
 * 有方向时滚动一屏左右：带 ref 滚动包含该元素的可滚动区域（虚拟列表只渲染可见部分，只有滚它才有后面的
 * 内容），不带 ref 滚动整页；滚不动报 `SCROLL_END`，让无限滚动的批次停下而不是空转。只有 ref 时把元素滚入
 * 视野（截图前定位用；点击与输入会自己滚）。不用 CDP 滚轮：视图不挂在窗口上时它永远不返回（实测）。
 */
export async function scroll(session: BrowserOperation, direction: "up" | "down" | undefined, ref: string | undefined): Promise<void> {
  if (direction === undefined) {
    await onNode(session, await element(session, ref!), "DOM.scrollIntoViewIfNeeded");
    return;
  }
  const down = direction === "down";
  const problem = ref
    ? await session.effect(async () => callOn(session, await element(session, ref), SCROLL_AREA, [down]))
    : await session.effect(() => evaluate(session, `(${SCROLL_AREA}).call(document.documentElement, ${down})`));
  if (problem) throw browserError("SCROLL_END", problem);
}

/** 原生下拉列表按值或可见文字选择；自定义下拉要先点开再点选项（工具描述里说明）。 */
export async function select(session: BrowserOperation, ref: string, values: readonly string[]): Promise<void> {
  const node = await element(session, ref);
  await actionable(session, node, false, false);
  const problem = await session.effect(() => callOn(session, node, SELECT_OPTIONS, [values]));
  if (problem) throw browserError("ACTION_FAILED", problem);
}
