import { ExternalLinkIcon, Globe2Icon } from "lucide-react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";

import { Collapsible, CollapsibleContent } from "@/components/ui/collapsible";
import { DisclosureRow, DisclosureRowStatic } from "../elements/disclosure-row.aui";
import { asRecord, isWebSearchData, readToolArtifact } from "./types";
import { ToolStatus } from "./tool-status";

export function WebSearchTool({ toolName, args, artifact: rawArtifact }: ToolCallMessagePartProps) {
  const artifact = readToolArtifact(rawArtifact);
  const data = isWebSearchData(artifact.data) ? artifact.data : null;
  const argsRecord = asRecord(args);
  const query = data?.query || (typeof argsRecord.query === "string" ? argsRecord.query : "网页");
  const results = data?.results ?? [];
  const expandable = artifact.presentation.expandable !== false && results.length > 0;
  const defaultOpen = artifact.presentation.default_open === true;

  const rowProps = {
    leading: <Globe2Icon className="text-muted-foreground size-4" aria-hidden="true" />,
    label: <span className="text-foreground text-sm font-medium">{artifact.presentation.verb ?? toolName}</span>,
    meta: (
      <>
        <span className="text-muted-foreground">{query}</span>
        {data && <span className="ml-2 text-muted-foreground">{results.length} 条结果</span>}
        {artifact.error && <span className="ml-2 text-destructive">{artifact.error}</span>}
      </>
    ),
    trailing: <ToolStatus status={artifact.backendStatus} />,
  };

  if (!expandable) {
    return <DisclosureRowStatic {...rowProps} />;
  }

  return (
    <Collapsible defaultOpen={defaultOpen} className="group/tool-call">
      <DisclosureRow {...rowProps} />
      <CollapsibleContent className="ml-6 space-y-1 pl-2 pt-1">
        {results.map((item) => (
          <a
            key={`${item.url}:${item.position ?? item.title}`}
            href={item.url}
            target="_blank"
            rel="noreferrer"
            className="group/result flex min-w-0 items-start gap-2 py-1 text-xs hover:text-foreground"
          >
            <ExternalLinkIcon className="text-muted-foreground mt-0.5 size-3 shrink-0" aria-hidden="true" />
            <span className="min-w-0">
              <span className="text-foreground block truncate font-medium">{item.title || item.url}</span>
              {item.description && <span className="text-muted-foreground line-clamp-2 block">{item.description}</span>}
              <span className="text-muted-foreground/70 block truncate">{item.url}</span>
            </span>
          </a>
        ))}
      </CollapsibleContent>
    </Collapsible>
  );
}
