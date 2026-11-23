/**
 * `display_data.kind = "next-step"` 的载荷（后端 `core/tool/handler/offer_next_step.py`）：回顾、一个问题与
 * 一到两个按钮标签，标签就是点击后替用户发出的话。回合布局在这里解析一次，卡片、痕迹与行高估算共用结果。
 */
export type NextStepOffer = { ask: string; replies: string[]; recap: string };

/** 展示数据来自持久化的工具结果：形状不对时返回 null，宁可不画也不让回合渲染出错。 */
export function readNextStepOffer(value: unknown): NextStepOffer | null {
  if (typeof value !== "object" || value === null) return null;
  const data = value as Record<string, unknown>;
  if (data.kind !== "next-step" || typeof data.ask !== "string" || typeof data.recap !== "string" || !Array.isArray(data.replies)) return null;
  const replies = data.replies.filter((reply): reply is string => typeof reply === "string" && reply.trim() !== "");
  return replies.length ? { ask: data.ask, replies, recap: data.recap } : null;
}
