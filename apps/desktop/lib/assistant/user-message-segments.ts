import type { ThreadUserMessage } from "@assistant-ui/react";

import { splitSkillMentions } from "@/lib/assistant/skill-mention-token";
import { decodeReferences, type WorkspaceReference } from "@/lib/workspace-references/codec";

export type UserMessageSegment =
  | { type: "text"; text: string }
  | { type: "reference"; reference: WorkspaceReference }
  | { type: "skill"; name: string }
  | { type: "file"; name: string };

/**
 * 已发送用户消息中除图片外各 part 的展示片段，是气泡里行内胶囊的唯一解码点。
 *
 * 输入是 canonical 形状（后端已把普通文件 token 在原位换成 file part，乐观消息由
 * converter 对齐同一形状），所以这里只识别正文里的引用链接与物化后的 Skill 标记。
 * 图片由气泡上方的预览区渲染，不在此产出；空白文本照常保留，避免胶囊两侧的空格被吞掉。
 */
export function userMessageSegments(parts: ThreadUserMessage["content"]): UserMessageSegment[] {
  return parts.flatMap((part): UserMessageSegment[] => {
    if (part.type === "file") return [{ type: "file", name: part.filename ?? "附件" }];
    if (part.type !== "text") return [];
    return decodeReferences(part.text).flatMap((piece): UserMessageSegment[] => (
      piece.type === "reference"
        ? [piece]
        : splitSkillMentions(piece.text).map((mention) => (
            mention.type === "token" ? { type: "skill", name: mention.token } : mention
          ))
    ));
  });
}
