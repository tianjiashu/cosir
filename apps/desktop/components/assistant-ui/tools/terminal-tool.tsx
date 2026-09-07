import { useState } from "react";
import { ChevronDownIcon, TerminalIcon } from "lucide-react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { asRecord, displayValue, readToolArtifact } from "./types";
import { ToolStatus } from "./tool-status";

export function TerminalTool({ toolName, args, result, artifact: rawArtifact }: ToolCallMessagePartProps) {
  const artifact = readToolArtifact(rawArtifact);
  const data = artifact.data ?? {};
  const command = typeof data.command === "string" ? data.command : typeof asRecord(args).command === "string" ? String(asRecord(args).command) : toolName;
  const output = typeof data.output === "string" ? data.output : result === undefined ? "" : displayValue(result);
  const exitCode = typeof data.exit_code === "number" ? data.exit_code : undefined;
  const defaultOpen = artifact.backendStatus === "running";
  const [open, setOpen] = useState(defaultOpen);

  return (
    <Collapsible open={open} onOpenChange={setOpen} className="group/tool-call overflow-hidden bg-zinc-950 text-zinc-100">
      <CollapsibleTrigger className="flex w-full items-center gap-2 py-1.5 text-left transition-colors hover:bg-white/5">
        <ChevronDownIcon className="size-4 shrink-0 text-zinc-400 transition-transform duration-200 group-data-open/tool-call:rotate-0 group-data-[state=closed]/tool-call:-rotate-90" aria-hidden="true" />
        <TerminalIcon className="size-4 shrink-0 text-zinc-400" aria-hidden="true" />
        <code className="min-w-0 flex-1 truncate text-xs">$ {command}</code>
        {exitCode !== undefined && <span className="text-xs text-zinc-400">exit {exitCode}</span>}
        {data.truncated === true && <span className="text-amber-300 text-xs">已截断</span>}
        <ToolStatus status={artifact.backendStatus} className="text-zinc-400" />
      </CollapsibleTrigger>
      <CollapsibleContent className="ml-6 border-t border-white/10 px-3 py-2">
        {artifact.error && <p className="mb-2 text-xs text-red-300">{artifact.error}</p>}
        {data.timed_out === true && <p className="mb-2 text-xs text-amber-300">命令执行超时</p>}
        {output && <pre className="max-h-72 overflow-auto whitespace-pre-wrap text-xs leading-relaxed text-zinc-300">{output}</pre>}
        {!output && !artifact.error && <p className="text-xs text-zinc-500">暂无终端输出</p>}
      </CollapsibleContent>
    </Collapsible>
  );
}
