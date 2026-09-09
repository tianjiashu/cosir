import { Trash2Icon } from "lucide-react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { DisclosureRowStatic } from "../elements/disclosure-row.aui";
import { asRecord, readToolArtifact } from "./types";
import { ToolStatus } from "./tool-status";

export function DeleteTool({ toolName, args, artifact: rawArtifact }: ToolCallMessagePartProps) {
  const artifact = readToolArtifact(rawArtifact);
  const data = artifact.data ?? {};
  const target = typeof data.path === "string" ? data.path : typeof args?.path === "string" ? args.path : "目标路径未知";
  const recursive = data.recursive === true || asRecord(args).recursive === true;
  return <DisclosureRowStatic
    leading={<Trash2Icon className="text-destructive size-4" aria-hidden="true" />}
    label={<span className="text-foreground text-sm font-medium">{artifact.presentation.verb ?? toolName}</span>}
    meta={<><span>{target}</span>{recursive && <span className="ml-2 text-muted-foreground">递归删除目录</span>}</>}
    trailing={<ToolStatus status={artifact.backendStatus} />}
  />;
}
