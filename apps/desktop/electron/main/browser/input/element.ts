import { browserError } from "../errors";
import type { PageNode } from "../ref-registry";
import type { PageSession } from "./session";

/**
 * 元素由快照里的 ref 指定：ref 按节点稳定、可能来自更早的快照，所以每次动作前都向页面确认节点还在文档里
 * （`element`）——已删除或已脱离文档的节点一律报 `REF_STALE`，不让焦点落空后把字输进别的元素。
 */

const IS_CONNECTED = `function () { return this.isConnected ? "" : "detached"; }`;

/** ref → 节点，并确认它还在文档里：删除的节点 CDP 报找不到，脱离文档但未回收的节点要问页面。 */
export async function element(session: PageSession, ref: string): Promise<PageNode> {
  const node = session.resolveRef(ref);
  if (await callOn(session, node, IS_CONNECTED)) {
    throw browserError("REF_STALE", `the element ${ref} is no longer on the page; use a ref from the latest snapshot`);
  }
  return node;
}

/** 对节点发一条按节点的命令（发到节点所在的会话）。 */
export function onNode<T = unknown>(session: PageSession, node: PageNode, method: string, params: Record<string, unknown> = {}): Promise<T> {
  return session.cdp<T>(method, { ...params, backendNodeId: node.backendNodeId }, { sessionId: node.sessionId });
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
  let quad: number[];
  try {
    quad = (await onNode<{ model: { border: number[] } }>(session, node, "DOM.getBoxModel")).model.border;
  } catch (error) {
    if (isMissingNode(error)) throw error;
    throw browserError("NOT_INTERACTABLE", "the element is not visible on the page");
  }
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

/** 在元素上执行一段固定的页面函数（不对外开放任意脚本）；约定返回空串表示成功，否则是问题说明。 */
export async function callOn(session: PageSession, node: PageNode, fn: string, args: unknown[] = []): Promise<string> {
  const { object } = await onNode<{ object: { objectId: string } }>(session, node, "DOM.resolveNode");
  const { result } = await session.cdp<{ result: { value?: unknown } }>("Runtime.callFunctionOn", {
    objectId: object.objectId,
    functionDeclaration: fn,
    arguments: args.map((value) => ({ value })),
    returnByValue: true,
  }, { sessionId: node.sessionId });
  return typeof result.value === "string" ? result.value : "";
}

/** 在页面（主框架）上执行一段固定的表达式；约定同 `callOn`。 */
export async function evaluate(session: PageSession, expression: string): Promise<string> {
  const { result } = await session.cdp<{ result: { value?: unknown } }>("Runtime.evaluate", { expression, returnByValue: true });
  return typeof result.value === "string" ? result.value : "";
}

/** 节点已不在：被删除，或它所在的子框架会话已分离（子框架导航换了进程或被移除）。 */
export function isMissingNode(error: unknown): boolean {
  return error instanceof Error && /No node (found|with given id)|Session with given id not found/i.test(error.message);
}
