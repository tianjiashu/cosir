import type { ThreadUserMessage } from "@assistant-ui/react";

import {
  parseCapsules,
  type FileCapsule,
  type RefCapsule,
  type SkillCapsule,
} from "@/lib/assistant/capsules";

export type UserMessageSegment =
  | { type: "text"; text: string }
  | { type: "file"; capsule: FileCapsule }
  | { type: "ref"; capsule: RefCapsule }
  | { type: "skill"; capsule: SkillCapsule };

/**
 * 已发送用户消息中除图片外各 part 的展示片段，是气泡里行内胶囊的唯一解码点。
 *
 * 消息里没有独立的文件 part：文件、引用、Skill 都是正文里的胶囊标签，输入框与已发送消息用同一套
 * 标签、同一份解析（`parseCapsules`），发送前后外观一致。图片由气泡上方的预览区渲染，不在此产出；
 * 空白文本照常保留，避免胶囊两侧的空格被吞掉。
 */
export function userMessageSegments(parts: ThreadUserMessage["content"]): UserMessageSegment[] {
  return parts.flatMap((part): UserMessageSegment[] => {
    if (part.type !== "text") return [];
    return parseCapsules(part.text).map((segment): UserMessageSegment => {
      if (segment.type === "text") return segment;
      switch (segment.capsule.kind) {
        case "file":
          return { type: "file", capsule: segment.capsule };
        case "ref":
          return { type: "ref", capsule: segment.capsule };
        case "skill":
          return { type: "skill", capsule: segment.capsule };
      }
    });
  });
}
