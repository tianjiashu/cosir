import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { asRecord, readToolArtifact } from "./types";
import { ToolCardShell } from "./tool-card-shell";
import { DETAIL_VIEWS } from "./details/detail-views";

/**
 * 通用低噪音工具卡：按 `display_data.kind` 从 `DETAIL_VIEWS` 取摘要与展开内容。
 *
 * 运行中或只有静态声明时还没有 kind，卡片只显示动词与状态；本组件不按工具名分支，
 * 也不从 `args` 推断展示内容。
 */
export function DetailsTool({ toolName, artifact: rawArtifact }: ToolCallMessagePartProps) {
  const artifact = readToolArtifact(rawArtifact);
  const data = asRecord(artifact.display_data);
  const view = typeof data.kind === "string" ? DETAIL_VIEWS[data.kind] : undefined;
  const body = view?.body?.(data);

  return (
    <ToolCardShell
      artifact={artifact}
      title={artifact.presentation.verb ?? toolName}
      summary={view?.summary(data, artifact.backendStatus)}
      body={body ? <div className="space-y-2 px-3 pb-3 text-sm">{body}</div> : undefined}
      defaultOpen={view !== undefined && artifact.presentation.default_open === true}
    />
  );
}
