/**
 * 全局设计 token（排版 / 间距等可复用原子值）。
 *
 * 与 `components/chat/messageTypography.ts` 的区别：本文件是**全局 leaf 级**
 * token，UI / 右栏 / 侧栏 / backend 各层组件均可引用，不引入跨层依赖。
 * `messageTypography.ts` 仅承载 chat 层消息体内特有的排版组合，二者不重叠。
 *
 * 字号 token 采用原子粒度（只描述字号本身），带 `font-medium` / `leading-*`
 * 等修饰的调用点自行在 class 字符串中追加，避免 token 语义被特定修饰污染。
 *
 * @module components/ui/tokens
 */

/** 小字（caption）token：收敛散落的 `text-[10px]` / `text-[11px]` 魔法字号。 */
export const Caption = {
  /** 11px：注释 / 元信息 / 标签等次要信息。 */
  xs: "text-[11px]",
  /** 11px 等宽：trace id、命令、token 计数等。 */
  mono: "font-mono text-[11px]",
  /** 10px：超小注释。 */
  xs10: "text-[10px]",
} as const;

/** 细线分隔原语：1px 视觉分隔，统一收口避免散落 `h-[1px]` / `w-[1px]` 魔法值。 */
export const Separator = {
  /** 横向分隔线：高度 1px、占满宽度。 */
  horizontal: "h-[1px] w-full",
  /** 纵向分隔线：宽度 1px、占满高度。 */
  vertical: "h-full w-[1px]",
} as const;

/** 滚动条内边距原语：1px 轨道留白，统一收口避免散落 `p-[1px]` 魔法值。 */
export const ScrollAreaPadding = {
  /** 滚动条轨道内边距：1px，纵向与横向共用。 */
  all: "p-[1px]",
} as const;

/** 面板折叠 / 代码块原语：折叠态高度上限，统一收口避免散落 `max-h-[480px]` 魔法值。 */
export const Panel = {
  /** 代码块 / 差异区折叠上限：480px，超出进入滚动。 */
  codeBlockMaxHeight: "max-h-[480px]",
} as const;

/** 代码行原语：最小行高，统一收口避免散落 `min-h-[1.5em]` 魔法值。 */
export const CodeLine = {
  /** 代码行最小高度：1.5em，保证空行也有可点击高度。 */
  minHeight: "min-h-[1.5em]",
} as const;
