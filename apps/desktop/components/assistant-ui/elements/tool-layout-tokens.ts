/** Shared layout geometry for assistant parts and tool renderers.
 *
 * These tokens own spacing at each layout level. Individual renderers may add
 * semantic content styling, but they must not add outer margins that compete
 * with the parent stack.
 */
export const ASSISTANT_PART_STACK_CLASS = "flex flex-col gap-1";

export const TOOL_TRACE_CONTENT_CLASS = "mt-1 flex flex-col gap-1";

/**
 * 终端类卡片（`execute_terminal` 与终端会话锚点卡）的外壳：`terminal-surface` 主题岛在两种主题下
 * 恒为深色，子元素照常用语义 token。`relative` 供失败时的左侧强调条定位。只给终端用——其他工具
 * 套上深色岛会和终端卡片混成一类。
 */
export const TERMINAL_CARD_CLASS = "terminal-surface relative overflow-hidden rounded-xl border shadow-sm";

/** 终端卡片表头行。与正文的分隔线由正文顶边承担，收起时不会和卡片边框叠成双线。 */
export const TERMINAL_CARD_HEADER_CLASS = "flex min-w-0 items-center gap-1 bg-muted/40 pr-2";

export const TOOL_CARD_ROW_CLASS = "flex min-h-7 items-start gap-2 py-1";

export const TOOL_DETAIL_ROW_CLASS =
  "flex min-h-7 items-center gap-2 px-2.5 py-1 text-xs";

export const TOOL_DETAIL_TEXT_ROW_CLASS = "min-h-7 truncate px-2.5 py-1 text-xs";
