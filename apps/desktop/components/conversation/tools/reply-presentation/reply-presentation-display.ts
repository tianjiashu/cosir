import { asRecord } from "../display-data-readers";

/** `present_files` 的一个交付物：`path` 用于显示与打开（工作区内为相对路径），`absolutePath` 只交给宿主定位。 */
export type DeliverableFile = { path: string; absolutePath: string; caption: string | null };

/** 读取交付物展示数据；形状不对的条目丢弃，一个都没有时返回 null、不画卡片。 */
export function readDeliverables(value: unknown): DeliverableFile[] | null {
  const data = asRecord(value);
  if (data.kind !== "deliverables" || !Array.isArray(data.files)) return null;
  const files = data.files.flatMap((item): DeliverableFile[] => {
    const file = asRecord(item);
    if (typeof file.path !== "string" || typeof file.absolute_path !== "string") return [];
    return [{ path: file.path, absolutePath: file.absolute_path, caption: typeof file.caption === "string" ? file.caption : null }];
  });
  return files.length ? files : null;
}

/** `offer_next_step` 的建议：一个问题与一到两个按钮标签（标签就是点击后替用户发出的话）。 */
export type NextStepOffer = { ask: string; replies: string[] };

export function readNextStep(value: unknown): NextStepOffer | null {
  const data = asRecord(value);
  if (data.kind !== "next-step" || typeof data.ask !== "string" || !Array.isArray(data.replies)) return null;
  const replies = data.replies.filter((reply): reply is string => typeof reply === "string" && reply.trim() !== "");
  return replies.length ? { ask: data.ask, replies } : null;
}
