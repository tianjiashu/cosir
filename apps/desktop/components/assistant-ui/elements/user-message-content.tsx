"use client";

import { useMemo } from "react";
import { MessagePrimitive, useAuiState, type ThreadUserMessage } from "@assistant-ui/react";
import { Streamdown } from "streamdown";

import { FileChip, SkillChip } from "@/components/composer/token-chips";
import { MarkdownText } from "@/components/markdown-text";
import { markdownPlugins, useMermaidOptions } from "@/components/markdown-rendering-config";
import { ReferenceChip } from "@/components/workspace-references/reference-chip";
import { userMessageSegments } from "@/lib/assistant/user-message-segments";

/**
 * 用户消息气泡内的正文与行内胶囊（图片除外，见 `UserMessageImages`）。
 *
 * 引用、Skill、普通文件按输入框里的同一组胶囊在原位渲染，发送前后外观一致；解码只在
 * `userMessageSegments` 一处。没有胶囊时整段走 `MarkdownText`，保留完整 Markdown 排版；
 * 有胶囊时按片段渲染，段落改为行内以便胶囊与文字同行（与输入框里的排版一致）。
 */
export function UserMessageContent() {
  // 本组件只挂在用户消息下，parts 恒为用户消息的内容形状。
  const parts = useAuiState((state) => state.message.parts) as ThreadUserMessage["content"];
  const segments = useMemo(() => userMessageSegments(parts), [parts]);
  const mermaidOptions = useMermaidOptions();

  if (segments.every((segment) => segment.type === "text")) {
    return (
      <MessagePrimitive.Parts>{({ part }) => (
        part.type === "text" ? <MarkdownText status={part.status} /> : null
      )}</MessagePrimitive.Parts>
    );
  }
  return (
    <div className="aui-md leading-6">
      {segments.map((segment, index) => {
        switch (segment.type) {
          case "text":
            return (
              <Streamdown
                key={index}
                mode="static"
                plugins={markdownPlugins}
                shikiTheme={["github-light", "github-dark"]}
                linkSafety={{ enabled: true }}
                mermaid={mermaidOptions}
                className="contents [&>p]:inline"
              >
                {segment.text}
              </Streamdown>
            );
          case "reference":
            return <ReferenceChip key={index} reference={segment.reference} />;
          case "skill":
            return <SkillChip key={index} name={segment.name} />;
          case "file":
            return <FileChip key={index} name={segment.name} description={segment.name} />;
        }
      })}
    </div>
  );
}

/** 气泡里是否有可渲染内容（非空白正文或普通文件）；只有图片的消息不画空气泡。 */
export function useHasUserMessageContent(): boolean {
  return useAuiState((state) => state.message.parts.some(
    (part) => (part.type === "text" && part.text.trim().length > 0) || part.type === "file",
  ));
}
