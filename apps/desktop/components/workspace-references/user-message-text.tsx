import { useMemo } from "react";
import type { MessagePartStatus } from "@assistant-ui/core";
import { Streamdown } from "streamdown";
import { MarkdownText } from "@/components/markdown-text";
import { markdownPlugins } from "@/components/markdown-rendering-config";
import { decodeReferences } from "@/lib/workspace-references/codec";
import { ReferenceChip } from "./reference-chip";

/** 消息正文保持原样；仅用户消息的展示投影折叠引用，Assistant 输出不被误认作用户选择。 */
export function UserMessageText({
  text,
  status,
}: {
  text: string;
  status?: MessagePartStatus;
}) {
  const parts = useMemo(() => decodeReferences(text), [text]);
  if (!parts.some((part) => part.type === "reference"))
    return <MarkdownText status={status} />;
  return (
    <div className="aui-md leading-6">
      {parts.map((part, index) =>
        part.type === "reference" ? (
          <ReferenceChip key={index} reference={part.reference} />
        ) : (
          <Streamdown
            key={index}
            mode="static"
            plugins={markdownPlugins}
            shikiTheme={["github-light", "github-dark"]}
            linkSafety={{ enabled: true }}
            mermaid={{ config: { securityLevel: "strict" } }}
            className="contents [&>p]:inline"
          >
            {part.text}
          </Streamdown>
        ),
      )}
    </div>
  );
}
