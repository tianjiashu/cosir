import { browserError } from "../errors";
import type { PageNode } from "../ref-registry";
import type { PageSession } from "./session";

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
export async function element(session: PageSession, ref: string): Promise<PageNode> {
  const node = session.resolveRef(ref);
  if (await callOn(session, node, IS_CONNECTED)) throw browserError("REF_STALE", STALE_ELEMENT);
  return node;
}

/** 对节点发一条按节点的命令（发到节点所在的会话）。 */
export async function onNode<T = unknown>(session: PageSession, node: PageNode, method: string, params: Record<string, unknown> = {}): Promise<T> {
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
export async function box(session: PageSession, node: PageNode): Promise<Box> {
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
export async function center(session: PageSession, node: PageNode): Promise<{ x: number; y: number }> {
  const { x, y, width, height } = await box(session, node);
  return { x: x + width / 2, y: y + height / 2 };
}

/** 在元素上执行一段固定的页面函数（不对外开放任意脚本），返回它给出的字符串；检查类函数约定空串表示成功、否则是问题说明。 */
export async function callOn(session: PageSession, node: PageNode, fn: string, args: unknown[] = []): Promise<string> {
  const { object } = await onNode<{ object: { objectId: string } }>(session, node, "DOM.resolveNode");
  return pageString(await elementCommand(session.cdp<PageCall>("Runtime.callFunctionOn", {
    objectId: object.objectId,
    functionDeclaration: fn,
    arguments: args.map((value) => ({ value })),
    returnByValue: true,
  }, { sessionId: node.sessionId })));
}

/** 在页面（主框架）上执行一段固定的表达式；约定同 `callOn`。 */
export async function evaluate(session: PageSession, expression: string): Promise<string> {
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
