import { TerminalIcon } from "lucide-react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { Collapsible, CollapsibleContent } from "@/components/ui/collapsible";
import { DisclosureRow } from "../elements/disclosure-row.aui";
import { readToolArtifact } from "./types";
import { ToolStatus } from "./tool-status";
import { useToolDisclosure } from "./tool-disclosure";

export function TerminalTool({ toolName, artifact: rawArtifact }: ToolCallMessagePartProps) {
  const artifact = readToolArtifact(rawArtifact);
  const data = artifact.data ?? {};
  const command = typeof data.command === "string" ? data.command : toolName;
  const output = typeof data.output === "string" ? data.output : "";
  const exitCode = typeof data.exit_code === "number" ? data.exit_code : undefined;
  const status = artifact.backendStatus;
  const [open, setOpen] = useToolDisclosure(status);

  return (
    <Collapsible open={open} onOpenChange={setOpen} className="group/tool-call overflow-hidden bg-zinc-950 text-zinc-100">
      <DisclosureRow
        leading={<TerminalIcon className="size-4 text-zinc-400" aria-hidden="true" />}
        label={<code className="text-sm">$ {command}</code>}
        meta={
          <>
            {exitCode !== undefined && <span className="text-zinc-400">exit {exitCode}</span>}
            {data.truncated === true && <span className="ml-2 text-amber-300">已截断</span>}
          </>
        }
        trailing={<ToolStatus status={artifact.backendStatus} className="text-zinc-400" />}
        tone="terminal"
      />
      <CollapsibleContent className="ml-6 px-3 py-2">
        {artifact.error && <p className="mb-2 text-xs text-red-300">{artifact.error}</p>}
        {data.timed_out === true && <p className="mb-2 text-xs text-amber-300">命令执行超时</p>}
        {output && <pre className="max-h-72 overflow-auto whitespace-pre-wrap text-xs leading-relaxed text-zinc-300">{output}</pre>}
        {!output && !artifact.error && <p className="text-xs text-zinc-500">暂无终端输出</p>}
      </CollapsibleContent>
    </Collapsible>
  );
}
