/**
 * 流式光标公共组件。
 *
 * AgentMessage / ThinkingBlock / CodeBlock 三处流式渲染都需要在内容尾部
 * 追加一个闪烁光标，此前各自手写 `<span className={cn(MessageTypography.caret, "ml-0.5")} />`，
 * 样式与无障碍属性存在漂移风险。此处统一收口为单一组件。
 *
 * @module components/chat/StreamingCaret
 */

import { cn } from "@/lib/utils";
import { MessageTypography } from "./messageTypography";

/** StreamingCaret 组件属性。 */
interface StreamingCaretProps {
  /** 是否显示光标；false / 省略时不渲染任何节点。 */
  show?: boolean;
}

/**
 * 流式光标元素。
 *
 * 目的:
 *   统一 caret 的样式、无障碍属性与测试标识，避免各消息组件重复书写类名；
 *   通过 `show` 收敛「是否处于流式末块」的条件判断，调用方无需再写三元表达式。
 *
 * 参数:
 *   show - 是否渲染光标，默认 false。
 *
 * 返回:
 *   `show` 为 true 时返回带 `data-testid="stream-caret"` 的装饰性 span；否则返回 null。
 *
 * 异常:
 *   不抛出异常。
 *
 * 副作用:
 *   无（纯渲染）。
 */
export function StreamingCaret({ show = false }: StreamingCaretProps) {
  if (!show) return null;
  return <span className={cn(MessageTypography.caret, "ml-0.5")} data-testid="stream-caret" aria-hidden />;
}
