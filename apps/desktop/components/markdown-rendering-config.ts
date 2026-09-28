import { code } from "@streamdown/code";
import { cjk } from "@streamdown/cjk";
import { math } from "@streamdown/math";
import { mermaid } from "@streamdown/mermaid";
import "katex/dist/katex.min.css";

/**
 * 对话 Markdown 的稳定插件配置。
 *
 * 该配置只声明 Streamdown 的解析与展示能力，不负责消息状态、流式传输或持久化。
 * 模块级单例保证消息 token 更新时不会重新创建插件对象，具体渲染仍由
 * `StreamdownTextPrimitive` 按当前消息 part 的状态决定。
 */
export const markdownPlugins = {
  code,
  math,
  mermaid,
  cjk,
} as const;
