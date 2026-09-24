/** Shared layout geometry for assistant parts and tool renderers.
 *
 * These tokens own spacing at each layout level. Individual renderers may add
 * semantic content styling, but they must not add outer margins that compete
 * with the parent stack.
 */
export const ASSISTANT_PART_STACK_CLASS = "flex flex-col gap-1";

export const TOOL_TRACE_CONTENT_CLASS = "mt-1 flex flex-col gap-1";

export const DARK_TOOL_CARD_HEADER_CLASS =
  "flex min-w-0 items-center gap-1 border-b border-white/5 bg-white/[0.02] pr-2";

export const DARK_TOOL_CARD_CONTENT_CLASS = "ml-6 pb-2 pl-2 pr-2";

export const TOOL_CARD_ROW_CLASS = "flex min-h-7 items-start gap-2 py-1";

export const TOOL_DETAIL_ROW_CLASS =
  "flex min-h-7 items-center gap-2 px-2.5 py-1 text-xs";

export const TOOL_DETAIL_TEXT_ROW_CLASS = "min-h-7 truncate px-2.5 py-1 text-xs";
