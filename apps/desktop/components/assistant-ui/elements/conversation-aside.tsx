import { createContext, useContext, type ReactNode, type RefObject } from "react";

/**
 * 对话附注：放在对话滚动容器里、与内容列同处一个网格的旁注（目前只有会话侧栏）。
 *
 * 为什么放进滚动容器而不是在外面另占一列：附注是对话画布的一部分（像文档站的「本页目录」），不是窗格。
 * 放在里面，滚动条留在视口最外侧，附注只占对话这块区域、不挤底部终端，也不用测量滚动条宽度。
 *
 * 网格是两列（内容 | 附注）：附注自己选位置——固定时占右列，浮起时与内容列叠在同一格右上角——并用
 * `sticky` 粘在视口顶部。附注拿到视口 ref，按视口宽度决定摆放、按视口高度限制自身高度；视口宽度不随
 * 附注的摆放变化，所以不会来回抖动。
 *
 * 由提供方决定有没有附注：对话页的任务外面提供，工作台的只读阅读器没有提供方，自然没有附注。
 */
export type ConversationAside = (viewportRef: RefObject<HTMLDivElement | null>) => ReactNode;

export const ConversationAsideContext = createContext<ConversationAside | null>(null);

/** 线程视口里的附注位置；只有它订阅上下文，附注开关不会让整个线程重渲染。 */
export function ConversationAsideSlot({ viewportRef }: { viewportRef: RefObject<HTMLDivElement | null> }) {
  const aside = useContext(ConversationAsideContext);
  return aside === null ? null : aside(viewportRef);
}
