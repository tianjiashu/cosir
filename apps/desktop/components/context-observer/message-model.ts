import type { ContextMessage } from "@/lib/api/context-messages";

export type MessageFilters = {
  search: string;
  role: string;
  inclusion: "all" | "included" | "excluded";
  sort: "sequence" | "created_at" | "updated_at";
  ascending: boolean;
};
export const INITIAL_FILTERS: MessageFilters = {
  search: "",
  role: "all",
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

export function roleLabel(role: string): string {
  return (
    (
      {
        system: "System",
        assistant: "Assistant",
        user: "User",
        human: "User",
        tool: "Tool",
      } as Record<string, string>
    )[role] ?? role
  );
}

export function valueText(value: unknown): string {
  return typeof value === "string"
    ? value
    : (JSON.stringify(value, null, 2) ?? "未提供");
}

export function toolNames(message: ContextMessage): string[] {
  return (message.tool_calls ?? []).flatMap((call) =>
    typeof call.name === "string" ? [call.name] : [],
  );
}

/** 多模态块只提取文字作摘要；原始结构由详情完整保留，不把附件载荷塞进列表。 */
export function contentPreview(content: unknown): string {
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

export function messagePreview(message: ContextMessage): string {
  return (
    contentPreview(message.content) ||
    (toolNames(message).length
      ? `调用 ${toolNames(message).join("、")}`
      : "（空内容）")
  );
}

/** 只索引已加载消息；与后端查询范围一致，不宣称搜索整个任务。 */
export function indexMessages(messages: ContextMessage[]) {
  return messages.map((message) => ({
    message,
    searchText: [
      message.sequence,
      `#${message.sequence}`,
      valueText(message.content),
      ...toolNames(message),
      message.tool_call_id ?? "",
    ]
      .join(" ")
      .toLocaleLowerCase(),
  }));
}

export function filterMessages(
  index: ReturnType<typeof indexMessages>,
  filters: MessageFilters,
): ContextMessage[] {
  const search = filters.search.trim().toLocaleLowerCase();
  return index
    .filter(
      ({ message, searchText }) =>
        (filters.role === "all" ||
          (filters.role === "user"
            ? ["human", "user"].includes(message.role)
            : message.role === filters.role)) &&
        (filters.inclusion === "all" ||
          message.include_in_context === (filters.inclusion === "included")) &&
        (!search || searchText.includes(search)),
    )
    .map((item) => item.message)
    .sort((left, right) => {
      const value = (message: ContextMessage) =>
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
  message: ContextMessage,
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

/** 变化仅对同一 Run 内可计算的相邻 Assistant 记录比较，不推断模型故障。 */
export function cacheChanges(messages: ContextMessage[]): Map<number, number> {
  const previous = new Map<number | null, number>();
  const result = new Map<number, number>();
  for (const message of [...messages].sort((a, b) => a.sequence - b.sequence)) {
    if (message.role !== "assistant") continue;
    const usage = cacheUsage(message);
    if (!usage) continue;
    const before = previous.get(message.run_id);
    if (before !== undefined)
      result.set(message.sequence, Math.round((usage.rate - before) * 10) / 10);
    previous.set(message.run_id, usage.rate);
  }
  return result;
}

/** 关联键包含 Run；仅导航到当前读取范围已有的记录，不制造工具执行状态。 */
export function relatedMessages(
  message: ContextMessage,
  messages: ContextMessage[],
): ContextMessage[] {
  if (message.role === "tool")
    return messages.filter(
      (candidate) =>
        candidate.role === "assistant" &&
        candidate.run_id === message.run_id &&
        candidate.tool_calls?.some((call) => call.id === message.tool_call_id),
    );
  const ids = new Set(
    (message.tool_calls ?? []).flatMap((call) =>
      typeof call.id === "string" ? [call.id] : [],
    ),
  );
  return messages.filter(
    (candidate) =>
      candidate.role === "tool" &&
      candidate.run_id === message.run_id &&
      candidate.tool_call_id &&
      ids.has(candidate.tool_call_id),
  );
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
