import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { readToolArtifact } from "./types";
import { ToolCardShell } from "./tool-card-shell";

/**
 * 没有专用 renderer 的工具（如 `wait_messages`）与未知、格式错误载荷的通用卡片：
 * 只展示动词、图标与状态，失败 / 取消短提示由外壳统一放在摘要位置。
 *
 * 不读取 `display_data`：走到这里说明载荷不可信或不需要展示。
 */
export function ToolFallback({ toolName, artifact: rawArtifact }: ToolCallMessagePartProps) {
  const artifact = readToolArtifact(rawArtifact);
  return <ToolCardShell artifact={artifact} title={artifact.presentation.verb ?? toolName} />;
}
