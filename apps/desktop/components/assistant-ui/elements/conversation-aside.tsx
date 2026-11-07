import { useThreadViewport } from "@assistant-ui/react";
import { createContext, useContext, type ReactNode } from "react";

import { useElementSize, type ElementSize } from "@/hooks/use-element-size";

/**
 * 对话附注：放在对话滚动容器里、与内容列同处一个网格的旁注（目前只有会话侧栏）。
 *
 * 为什么放进滚动容器而不是在外面另占一列：附注是对话画布的一部分（像文档站的「本页目录」），不是窗格。
 * 放在里面，滚动条留在视口最外侧，附注只占对话这块区域、不挤底部终端，也不用测量滚动条宽度。
 *
 * 网格是两列（内容 | 附注）：附注自己选位置——固定时占右列，浮起时与内容列叠在同一格右上角——并用
 * `sticky` 粘在视口顶部。附注只消费视口尺寸，按宽度决定摆放、按高度限制自身高度；视口宽度不随
 * 附注的摆放变化，所以不会来回抖动。
 *
 * 由提供方决定有没有附注：对话页的任务外面提供，工作台的只读阅读器没有提供方，自然没有附注。
 */
export type ConversationAside = (viewport: ElementSize) => ReactNode;

export const ConversationAsideContext = createContext<ConversationAside | null>(null);

/**
 * 线程视口里的附注位置，也是附注的尺寸边界：订阅 primitive 注册的实际元素，绑定后才观察尺寸。
 * 不读取祖先 ref 猜测挂载时机；开关与尺寸更新都留在本组件，不让整个线程随 resize 重渲染。
 * 没有附注时停止尺寸观察。库的滚动状态只提供 clientHeight，这里测量布局所需的完整宽高。
 */
export function ConversationAsideSlot() {
  const aside = useContext(ConversationAsideContext);
  const element = useThreadViewport((state) => state.element.viewport);
  const [, viewport] = useElementSize(aside === null ? null : element);
  return aside === null ? null : aside(viewport);
}
