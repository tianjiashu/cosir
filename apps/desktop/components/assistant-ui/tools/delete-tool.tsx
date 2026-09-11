import { Trash2Icon } from "lucide-react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { DisclosureRowStatic } from "../elements/disclosure-row.aui";
import { readToolArtifact } from "./types";
import { ToolStatus } from "./tool-status";

export function DeleteTool({ toolName, artifact: rawArtifact }: ToolCallMessagePartProps) {
  const artifact = readToolArtifact(rawArtifact);
  const data = artifact.display_data ?? {};
  const isTerminalState = artifact.backendStatus === "failed" || artifact.backendStatus === "cancelled";
  const target = isTerminalState
    ? null
    : typeof data.path === "string" ? data.path : "目标路径未知";
  const recursive = data.recursive === true;
  const targetType = typeof data.target_type === "string" ? data.target_type : "target";
  const mode = recursive
    ? "递归删除"
    : targetType === "directory"
      ? "删除目录"
      : targetType === "link"
        ? "删除链接"
        : "删除文件";
  return <DisclosureRowStatic
    leading={<Trash2Icon className="text-destructive size-4" aria-hidden="true" />}
    label={<span className="text-foreground text-sm font-medium">{artifact.presentation.verb ?? toolName}</span>}
    meta={isTerminalState ? <span className="text-destructive">{artifact.error ?? (artifact.backendStatus === "cancelled" ? "已取消" : "执行失败")}</span> : <><span>{target}</span><span className="ml-2 text-muted-foreground">{mode}</span></>}
    trailing={<ToolStatus status={artifact.backendStatus} />}
  />;
}
