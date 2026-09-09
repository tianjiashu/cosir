import { CheckIcon, CircleAlertIcon, CircleDashedIcon, LoaderCircleIcon, XCircleIcon } from "lucide-react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";

import { DisclosureRowStatic } from "../elements/disclosure-row.aui";
import { isWebExtractStatusData, readToolArtifact, resolveWebExtractSiteStatus } from "./types";
import { ToolStatus } from "./tool-status";

function SiteStatus({ status }: { status: string }) {
  const Icon = status === "success" ? CheckIcon
    : status === "failed" ? CircleAlertIcon
      : status === "truncated" ? CircleAlertIcon
        : status === "running" ? LoaderCircleIcon
          : status === "pending" ? CircleDashedIcon : XCircleIcon;
  const className = status === "failed" ? "text-destructive" : status === "truncated" ? "text-amber-600" : "text-muted-foreground";
  const label = status === "success" ? "已提取" : status === "failed" ? "失败" : status === "truncated" ? "已截断" : status === "running" ? "提取中" : status === "pending" ? "等待中" : "已取消";
  return (
    <span className={`inline-flex shrink-0 items-center gap-1 text-xs ${className}`}>
      <Icon className={`size-3 ${status === "running" ? "animate-spin" : ""}`} aria-hidden="true" />
      {label}
    </span>
  );
}

export function WebExtractStatusTool({ toolName, artifact: rawArtifact }: ToolCallMessagePartProps) {
  const artifact = readToolArtifact(rawArtifact);
  const data = isWebExtractStatusData(artifact.data) ? artifact.data : null;
  const sites = data?.sites ?? [];

  if (sites.length === 0) {
    return <DisclosureRowStatic
      label={<span className="text-foreground text-sm font-medium">{artifact.presentation.verb ?? toolName}</span>}
      trailing={<ToolStatus status={artifact.backendStatus} />}
    />;
  }

  return (
    <div className="space-y-0.5 py-1.5">
      {sites.map((site) => (
        <div key={site.url} className="flex min-w-0 items-center gap-2 text-sm">
          <span className="text-foreground min-w-0 truncate font-medium">{site.site}</span>
          <SiteStatus status={resolveWebExtractSiteStatus(site.status, artifact.backendStatus)} />
        </div>
      ))}
    </div>
  );
}
