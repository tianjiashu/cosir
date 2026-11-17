import { asRecord } from "../display-data-readers";

/** `offer_next_step` 的建议：一个问题与一到两个按钮标签（标签就是点击后替用户发出的话）。 */
export type NextStepOffer = { ask: string; replies: string[] };

export function readNextStep(value: unknown): NextStepOffer | null {
  const data = asRecord(value);
  if (data.kind !== "next-step" || typeof data.ask !== "string" || !Array.isArray(data.replies)) return null;
  const replies = data.replies.filter((reply): reply is string => typeof reply === "string" && reply.trim() !== "");
  return replies.length ? { ask: data.ask, replies } : null;
}
