// 消息排版 token：所有消息组件复用，禁止各写一套 text-sm leading-relaxed
export const MessageTypography = {
  body: "text-[13px] leading-7", // 正文：介于 sm(14) 与 xs(12) 之间，行距 28px 透气
  secondary: "text-xs leading-5", // 思考块 / 代码块 / 元信息：稳定次要层级
  code: "text-xs leading-5 font-mono", // 等宽代码
  caret: "inline-block w-[1px] h-[1em] align-text-bottom animate-pulse bg-foreground/70", // 流式光标
} as const;
