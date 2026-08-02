/**
 * 工具展示契约（前后端共享的静态声明类型）。
 *
 * 本模块只定义「工具在客户端长什么样」的**静态声明**，不含任何渲染逻辑。
 * 后端 `ToolDefinition.display` 按同名 snake_case 字段透传这份声明，客户端负责
 * 把声明 + 运行时数据渲染成界面（渲染规则见 `toolDisplayRules`）。
 *
 * 边界：
 * - 只有字面量字段，无函数、无摘要文本、无条目投影。
 * - 后端不产出任何展示文本；一切摘要与条目均在客户端生成。
 *
 * @module shared/toolDisplay
 */

/** 展开态布局类型；客户端按该值分发布局，不按工具名写特化分支。 */
export type ToolExpandLayout = "none" | "details" | "list" | "diff" | "write" | "terminal";

/** 后端透传的工具展示静态声明（snake_case，与后端 dataclass 字段一一对应）。 */
export interface ToolDisplayPayload {
  /** 动作名，如 “读取”，客户端作为主标题动词。 */
  verb?: unknown;
  /** lucide 图标名，如 “eye”。 */
  icon?: unknown;
  /** 是否可展开。 */
  expandable?: unknown;
  /** 展开态布局字符串。 */
  expand_layout?: unknown;
}

/** 客户端使用的工具展示静态声明（camelCase，字段均已收窄且有默认值）。 */
export interface ToolDisplayHints {
  /** 动作名，如 “读取”。 */
  verb: string;
  /** lucide 图标名，如 “eye”。 */
  icon: string;
  /** 是否可展开。 */
  expandable: boolean;
  /** 展开态布局。 */
  expandLayout: ToolExpandLayout;
}

/** 合法展开布局集合，用于收窄后端透传的任意字符串。 */
const EXPAND_LAYOUTS: readonly ToolExpandLayout[] = [
  "none",
  "details",
  "list",
  "diff",
  "write",
  "terminal",
];

/** 后端未声明 display 时使用的兜底展示声明。 */
export const DEFAULT_TOOL_DISPLAY_HINTS: ToolDisplayHints = {
  verb: "",
  icon: "wrench",
  expandable: true,
  expandLayout: "details",
};

/**
 * 把后端透传的展示声明收窄为客户端展示声明。
 *
 * @param raw - 事件 payload 中的 `display` 字段，可能为 undefined / null / 非对象。
 * @returns 字段完整的 `ToolDisplayHints`；输入非法时返回默认声明。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
export function toToolDisplayHints(raw: unknown): ToolDisplayHints {
  if (!raw || typeof raw !== "object") {
    return DEFAULT_TOOL_DISPLAY_HINTS;
  }
  const payload = raw as ToolDisplayPayload;
  const layout = payload.expand_layout;
  return {
    verb: typeof payload.verb === "string" ? payload.verb : DEFAULT_TOOL_DISPLAY_HINTS.verb,
    icon: typeof payload.icon === "string" ? payload.icon : DEFAULT_TOOL_DISPLAY_HINTS.icon,
    expandable:
      typeof payload.expandable === "boolean"
        ? payload.expandable
        : DEFAULT_TOOL_DISPLAY_HINTS.expandable,
    expandLayout: isExpandLayout(layout) ? layout : DEFAULT_TOOL_DISPLAY_HINTS.expandLayout,
  };
}

/**
 * 判断任意值是否为合法展开布局字符串。
 *
 * @param value - 待判定值。
 * @returns 是合法布局时返回 true（类型收窄为 `ToolExpandLayout`）。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
function isExpandLayout(value: unknown): value is ToolExpandLayout {
  return typeof value === "string" && (EXPAND_LAYOUTS as readonly string[]).includes(value);
}
