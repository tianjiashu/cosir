import type { ObservedMessage } from "@/lib/api/session-observation";

export type MessageFilters = {
  search: string;
  /** `messageKind` 的取值，或 "all"。 */
  kind: string;
  inclusion: "all" | "included" | "excluded";
  sort: "sequence" | "created_at" | "updated_at";
  ascending: boolean;
};
export const INITIAL_FILTERS: MessageFilters = {
  search: "",
  kind: "all",
  inclusion: "all",
  sort: "sequence",
  ascending: true,
};

export function parseId(value: string | null): number | undefined {
  if (!value) return undefined;
  return /^\d+$/.test(value) &&
    Number.isSafeInteger(Number(value)) &&
    Number(value) > 0
    ? Number(value)
    : undefined;
}

/**
 * 一行「是什么」：进模型时用户输入、系统通知、Agent 消息都是 user 角色，只按角色分不清来源，
 * 所以 user 行按后端记下的种类分，其余角色各是一种。没有种类的 user 行退回角色。
 */
export const MESSAGE_KINDS: ReadonlyArray<readonly [string, string]> = [
  ["system", "系统提示词"],
  ["user_input", "用户输入"],
  ["agent_message", "Agent 消息"],
  ["notice", "系统通知"],
  ["assistant", "模型回复"],
  ["tool", "工具结果"],
  ["decision", "用户决定"],
  ["tool_result", "决定后的结果"],
  ["background_result", "后台结论"],
  ["tool_image", "工具图片"],
];
const KIND_LABELS = new Map(MESSAGE_KINDS);

export function messageKind(message: ObservedMessage): string {
  return message.role === "user" ? (message.kind ?? "user") : message.role;
}

export function kindLabel(message: ObservedMessage): string {
  const kind = messageKind(message);
  return KIND_LABELS.get(kind) ?? kind;
}

/** 这一行涉及的工具：工具结果与回答调用的行是那次调用的工具，模型回复是它发起的调用。 */
export function messageTools(message: ObservedMessage): string[] {
  return message.tool_name ? [message.tool_name] : toolNames(message);
}

/** 行标题：种类加涉及的工具，列表行、无障碍名称与详情标题共用。 */
export function messageTitle(message: ObservedMessage): string {
  const tools = messageTools(message);
  return tools.length ? `${kindLabel(message)} · ${tools.join("、")}` : kindLabel(message);
}

/**
 * 是否进了模型上下文，以及没进的原因。原因不落库，按存储标记推导：被撤回的行两个标记都关掉；
 * 其余没进的是流式中断或没通过工具准入的模型输出及其结果。标记反映现在的状态，撤回的行此前进过。
 */
export function contextState(message: ObservedMessage): { included: boolean; label: string } {
  if (message.include_in_context) return { included: true, label: "进了上下文" };
  return { included: false, label: message.withdrawn ? "没进上下文 · 已撤回" : "没进上下文 · 未采纳" };
}

export function valueText(value: unknown): string {
  return typeof value === "string"
    ? value
    : (JSON.stringify(value, null, 2) ?? "未提供");
}

function toolNames(message: ObservedMessage): string[] {
  return (message.tool_calls ?? []).flatMap((call) =>
    typeof call.name === "string" ? [call.name] : [],
  );
}

/** 多模态块只提取文字作摘要；原始结构由详情完整保留，不把附件载荷塞进列表。 */
function contentPreview(content: unknown): string {
  if (typeof content === "string")
    return content.replace(/\s+/g, " ").slice(0, 180);
  if (Array.isArray(content))
    return content
      .map((block) => {
        if (typeof block === "string") return block;
        if (block && typeof block === "object")
          return typeof block.text === "string"
            ? block.text
            : `[${block.type ?? "内容块"}]`;
        return "";
      })
      .join(" ")
      .replace(/\s+/g, " ")
      .slice(0, 180);
  return valueText(content).replace(/\s+/g, " ").slice(0, 180);
}

/** 队列消息落库时包着一层来源标签（`<user_input time=… hint=…>`），摘要只留正文，原文在详情里。 */
function unwrapped(preview: string): string {
  return preview
    .replace(/^<[a-z_]+(\s[^>]*)?>\s*/, "")
    .replace(/\s*<\/[a-z_]+>$/, "");
}

export function messagePreview(message: ObservedMessage): string {
  const preview = contentPreview(message.content);
  return (
    (message.role === "user" ? unwrapped(preview) : preview) ||
    (toolNames(message).length
      ? `调用 ${toolNames(message).join("、")}`
      : "（空内容）")
  );
}

/** 只索引已加载消息；与后端查询范围一致，不宣称搜索整个会话。 */
export function indexMessages(messages: ObservedMessage[]) {
  return messages.map((message) => ({
    message,
    searchText: [
      message.sequence,
      `#${message.sequence}`,
      valueText(message.content),
      kindLabel(message),
      ...messageTools(message),
      message.tool_call_id ?? "",
    ]
      .join(" ")
      .toLocaleLowerCase(),
  }));
}

export function filterMessages(
  index: ReturnType<typeof indexMessages>,
  filters: MessageFilters,
): ObservedMessage[] {
  const search = filters.search.trim().toLocaleLowerCase();
  return index
    .filter(
      ({ message, searchText }) =>
        (filters.kind === "all" || messageKind(message) === filters.kind) &&
        (filters.inclusion === "all" ||
          message.include_in_context === (filters.inclusion === "included")) &&
        (!search || searchText.includes(search)),
    )
    .map((item) => item.message)
    .sort((left, right) => {
      const value = (message: ObservedMessage) =>
        filters.sort === "sequence"
          ? message.sequence
          : Date.parse(message[filters.sort]);
      return (
        (value(left) - value(right) || left.sequence - right.sequence) *
        (filters.ascending ? 1 : -1)
      );
    });
}

export function formatTime(value: string, full = false): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(
    "zh-CN",
    full
      ? {
          year: "numeric",
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
          fractionalSecondDigits: 3,
          hourCycle: "h23",
        }
      : {
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
          hourCycle: "h23",
        },
  ).format(date);
}

export function cacheUsage(
  message: ObservedMessage,
): { rate: number; hit: number; input: number } | null {
  const usage = message.usage_metadata;
  const details = usage?.input_token_details;
  const input = usage?.input_tokens;
  const hit =
    details && typeof details === "object"
      ? (details as Record<string, unknown>).cache_read
      : undefined;
  if (
    typeof input !== "number" ||
    !Number.isFinite(input) ||
    input <= 0 ||
    typeof hit !== "number" ||
    !Number.isFinite(hit) ||
    hit < 0 ||
    hit > input
  )
    return null;
  return { rate: Math.round((hit / input) * 1000) / 10, hit, input };
}

/** 变化只对本地（非继承）相邻、可计算的 Assistant 记录比较，不推断模型故障。 */
export function cacheChanges(messages: ObservedMessage[]): Map<number, number> {
  let previous: number | undefined;
  const result = new Map<number, number>();
  for (const message of [...messages].sort((a, b) => a.sequence - b.sequence)) {
    if (message.role !== "assistant" || message.inherited) continue;
    const usage = cacheUsage(message);
    if (!usage) continue;
    if (previous !== undefined)
      result.set(message.sequence, Math.round((usage.rate - previous) * 10) / 10);
    previous = usage.rate;
  }
  return result;
}

/** 这一行提到的调用：自己回答的那次、自己发起的那些，以及系统通知正文里点名的（如「提问未被回答」）。 */
function callIds(message: ObservedMessage, known: ReadonlySet<string>): string[] {
  const ids = (message.tool_calls ?? []).flatMap(call => typeof call.id === "string" ? [call.id] : []);
  if (message.tool_call_id) ids.push(message.tool_call_id);
  if (message.kind === "notice" && typeof message.content === "string") {
    const content = message.content;
    ids.push(...[...known].filter(id => content.includes(id)));
  }
  return ids;
}

/**
 * 同一次调用的其他记录：`tool_call_id` 在 Session 内唯一（含继承来的记录）。一次调用除了发起它的
 * 模型回复和工具结果，还可能有回答它的 user 行（用户决定、后台结论）与点名它的系统通知，而且可以
 * 在后续回合才并入。通知只在正文里写调用 id，没有结构化字段，所以按正文匹配已知的调用 id。
 */
export function relatedMessages(
  message: ObservedMessage,
  messages: ObservedMessage[],
): ObservedMessage[] {
  const known = new Set(messages.flatMap(item => item.tool_call_id ? [item.tool_call_id] : []));
  const ids = new Set(callIds(message, known));
  if (!ids.size) return [];
  return messages.filter(candidate =>
    candidate !== message && callIds(candidate, known).some(id => ids.has(id)));
}

export function toolStatus(status: string | null | undefined): string {
  return status
    ? ((
        {
          completed: "已完成",
          failed: "失败",
          cancelled: "已取消",
          running: "执行中",
          pending: "待执行",
        } as Record<string, string>
      )[status] ?? status)
    : "状态未记录";
}
