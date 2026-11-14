import { createContext, type ReactNode } from "react";

/**
 * 空对话的外框：对话还没有任何消息时，由提供方决定输入框摆在什么样的版面里。
 *
 * 有提供方时线程把输入框交给它、不渲染自己的视口（没有消息可滚动），加载中的骨架同样交给它（`ThreadSkeleton`），
 * 所以版面只有一份实现。目前唯一的提供方是 AppLayout：未开始的会话就是新会话页，用新会话页的外框
 * （`NewConversationFrame`）；其余对话没有提供方，空态是线程默认的居中输入框。
 */
export type EmptyThreadFrame = (composer: ReactNode) => ReactNode;

export const EmptyThreadFrameContext = createContext<EmptyThreadFrame | null>(null);
