import { browserError } from "../errors";
import type { PageNode } from "../ref-registry";
import type { BrowserOperation } from "../operation";

/**
 * 元素由快照里的 ref 指定：ref 按节点稳定、可能来自更早的快照，所以每次动作前都向页面确认节点还在文档里
 * （`element`）——已删除或已脱离文档的节点一律报 `REF_STALE`，不让焦点落空后把字输进别的元素。
 *
 * 按节点的命令都经 `onNode` 发出，元素本身的问题只在这里认、换成错误码，调用方不再各自判断：CDP 报找不到节点（删除）
 * 或会话已不在（子框架导航换了进程、被移除）为 `REF_STALE`，节点没有布局（隐藏、`display: none`）为 `NOT_INTERACTABLE`；
 * 页面停住、超时等照原样上报。脱离文档但未回收的节点由 `element` 问页面。
 *
 * 固定页面函数（`callOn` / `evaluate`）的结局也只在这里解释：CDP 对页面里抛出的异常不报错，而是在响应里带
 * `exceptionDetails`——不看它就会把「页面函数抛错」当成「返回空串 = 成功」（页面改写了内置对象时会发生）。
 */

const IS_CONNECTED = `function () { return this.isConnected ? "" : "detached"; }`;
const STALE_ELEMENT = "the element is no longer on the page; use a ref from the latest snapshot";

/** ref → 节点，并确认它还在文档里。 */
export async function element(session: BrowserOperation, ref: string): Promise<PageNode> {
  const node = session.resolveRef(ref);
  if (await callOn(session, node, IS_CONNECTED)) throw browserError("REF_STALE", STALE_ELEMENT);
  return node;
}

/** 对节点发一条按节点的命令（发到节点所在的会话）。 */
export async function onNode<T = unknown>(session: BrowserOperation, node: PageNode, method: string, params: Record<string, unknown> = {}): Promise<T> {
  return elementCommand(session.cdp<T>(method, { ...params, backendNodeId: node.backendNodeId }, { sessionId: node.sessionId }));
}

/** 元素命令的失败换成错误码（见模块说明）；其余照原样上报。 */
async function elementCommand<T>(command: Promise<T>): Promise<T> {
  try {
    return await command;
  } catch (error) {
    if (!(error instanceof Error)) throw error;
    if (MISSING_NODE.test(error.message)) throw browserError("REF_STALE", STALE_ELEMENT);
    if (NO_LAYOUT.test(error.message)) throw browserError("NOT_INTERACTABLE", "the element is not visible on the page");
    throw error;
  }
}

/** 主视口里的一块矩形（CSS 像素）。 */
export type Box = { x: number; y: number; width: number; height: number };

/**
 * 元素的边框框（主视口的 CSS 像素）：先滚动到可见再量；没有布局（隐藏、零尺寸）报 `NOT_INTERACTABLE`。跨进程子框架里
 * 的坐标是框架内的，加上框架的偏移（`frameOffset` 顺带把外层的 iframe 滚入视野，不影响框架内坐标）。动作的落点与
 * 元素截图都从这里取，量的是此刻的位置；快照时成批的几何另走 `DOMSnapshot`（见 `snapshot/layout.ts`）。
 */
export async function box(session: BrowserOperation, node: PageNode): Promise<Box> {
  await onNode(session, node, "DOM.scrollIntoViewIfNeeded");
  const offset = await session.frameOffset(node.sessionId);
  const quad = (await onNode<{ model: { border: number[] } }>(session, node, "DOM.getBoxModel")).model.border;
  const xs = [quad[0], quad[2], quad[4], quad[6]];
  const ys = [quad[1], quad[3], quad[5], quad[7]];
  const x = Math.min(...xs);
  const y = Math.min(...ys);
  const width = Math.max(...xs) - x;
  const height = Math.max(...ys) - y;
  if (width < 1 || height < 1) throw browserError("NOT_INTERACTABLE", "the element has no size on the page");
  return { x: offset.x + x, y: offset.y + y, width, height };
}

/** 元素中心：动作的落点。 */
export async function center(session: BrowserOperation, node: PageNode): Promise<{ x: number; y: number }> {
  const { x, y, width, height } = await box(session, node);
  return { x: x + width / 2, y: y + height / 2 };
}

/** 在元素上执行一段只读页面函数（不对外开放任意脚本），返回它给出的字符串；检查类函数约定空串表示成功、否则是问题说明。 */
export async function callOn(session: BrowserOperation, node: PageNode, fn: string, args: unknown[] = []): Promise<string> {
  return pageString(await runOnPage(session, await resolveObject(session, node), node.sessionId, fn, args));
}

/**
 * 执行可能改变页面状态的页面函数：解析节点是只读的（在 effect 外），只有 `Runtime.callFunctionOn`
 * 进入 effect。这样节点已不在、或命令在派发前被闸门拒绝时，不会被误记成「可能已生效」。
 */
export async function mutateOn(session: BrowserOperation, node: PageNode, fn: string, args: unknown[] = []): Promise<string> {
  const objectId = await resolveObject(session, node);
  return session.effect(async () => pageString(await runOnPage(session, objectId, node.sessionId, fn, args)));
}

async function resolveObject(session: BrowserOperation, node: PageNode): Promise<string> {
  const { object } = await onNode<{ object: { objectId: string } }>(session, node, "DOM.resolveNode");
  return object.objectId;
}

function runOnPage(session: BrowserOperation, objectId: string, sessionId: string | undefined, fn: string, args: unknown[]): Promise<PageCall> {
  return elementCommand(session.cdp<PageCall>("Runtime.callFunctionOn", {
    objectId,
    functionDeclaration: fn,
    arguments: args.map((value) => ({ value })),
    returnByValue: true,
    awaitPromise: true,
  }, { sessionId }));
}

/** 在页面（主框架）上执行一段固定的表达式；约定同 `callOn`。 */
export async function evaluate(session: BrowserOperation, expression: string): Promise<string> {
  return pageString(await session.cdp<PageCall>("Runtime.evaluate", { expression, returnByValue: true }));
}

type PageCall = { result: { value?: unknown }; exceptionDetails?: { text: string; exception?: { description?: string } } };

/**
 * 页面函数的结局：抛错是这次动作失败（`ACTION_FAILED`，带页面给的说明，模型据此换个做法）；返回的不是字符串是本仓
 * 页面函数写错了，抛普通错误暴露出来，不兜底成空串（那会被当成成功）。
 */
function pageString({ result, exceptionDetails }: PageCall): string {
  if (exceptionDetails) {
    const reason = (exceptionDetails.exception?.description ?? exceptionDetails.text).split("\n")[0].slice(0, 300);
    throw browserError("ACTION_FAILED", `the page raised an error: ${reason}`);
  }
  if (typeof result.value !== "string") throw new Error(`a page function returned ${typeof result.value} instead of a string`);
  return result.value;
}

/** CDP 对「节点已删除」与「会话已分离」的报错（实测文案）。 */
const MISSING_NODE = /No node (found|with given id)|Session with given id not found/i;
/** CDP 对没有布局的节点的报错：滚入视野是「没有布局对象」（实测），取盒模型是「算不出盒模型」。 */
const NO_LAYOUT = /does not have a layout object|Could not compute box model/i;

/** 检查只在输入前执行一次；失败让模型重新观察，不通过重放输入试探页面。 */
export async function actionable(session: BrowserOperation, node: PageNode, editable = false, pointer = true): Promise<{ x: number; y: number }> {
  await box(session, node);
  const problem = await callOn(session, node, ACTIONABLE, [editable, pointer]);
  if (problem) throw browserError("NOT_INTERACTABLE", problem);
  const area = await box(session, node);
  return { x: area.x + area.width / 2, y: area.y + area.height / 2 };
}

const ACTIONABLE = `async function (editable, pointer) {
  if (!this.isConnected) return "the element is detached";
  if (this.matches(":disabled") || this.closest('[aria-disabled="true"], [inert]')) return "the element is disabled";
  if (editable && (this.readOnly || !(this.isContentEditable || this.tagName === "TEXTAREA" || (this.tagName === "INPUT" && !["button","submit","checkbox","radio","file","hidden"].includes(this.type))))) return "the element is not editable";
  let problem = "the element is moving";
  for (let attempt = 0; attempt < 30; attempt++) {
    const before = this.getBoundingClientRect();
    await new Promise(resolve => setTimeout(resolve, 50));
    const after = this.getBoundingClientRect();
    if (!this.isConnected) return "the element is detached";
    if (["x","y","width","height"].some(k => Math.abs(before[k] - after[k]) > 0.5)) { problem = "the element is moving"; continue; }
    if (getComputedStyle(this).visibility !== "visible") return "the element is hidden";
    if (pointer) {
      const hit = this.getRootNode().elementFromPoint(after.x + after.width / 2, after.y + after.height / 2);
      if (!hit || !(hit === this || this.contains(hit))) { problem = "the element is covered at its input point"; continue; }
    }
    return "";
  }
  return problem;
}`;
