import { TerminalIcon, TriangleAlertIcon } from "lucide-react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { cn } from "@/lib/utils";
import { Collapsible, CollapsibleContent } from "@/components/ui/collapsible";
import { DisclosureRow } from "../elements/disclosure-row.aui";
import { asRecord, readToolArtifact } from "./types";
import { ToolStatus } from "./tool-status";
import { useToolDisclosure } from "./tool-disclosure";

/** 显式 shell 的徽标文案；`auto` 沿用宿主默认 shell，不展示徽标。 */
const SHELL_LABELS: Record<string, string> = {
  cmd: "cmd",
  powershell: "ps",
  pwsh: "pwsh",
  sh: "sh",
  bash: "bash",
  zsh: "zsh",
  fish: "fish",
};

/** 从任意未知值中读取非空字符串，缺失、空串或类型不符时返回 undefined。 */
function text(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

/**
 * 把后端 shell 值映射为徽标文案。
 *
 * `auto` 与缺失都表示宿主默认 shell，返回 null 以避免无信息量的徽标。
 */
function shellBadge(value: unknown): string | null {
  return typeof value === "string" ? SHELL_LABELS[value] ?? null : null;
}

/**
 * 以嵌入式深色终端卡片渲染 execute_terminal 调用。
 *
 * 命令与 shell 优先取自工具参数：参数在 running 态即随状态事件下发，早于子进程
 * 真正执行，因此命令能在执行前就渲染出来。输出与退出码来自 terminal 终态
 * display_data，仅在工具结束后可用。
 */
export function TerminalTool({ toolName, args, artifact: rawArtifact }: ToolCallMessagePartProps) {
  const artifact = readToolArtifact(rawArtifact);
  const data = artifact.display_data ?? {};
  const argRecord = asRecord(args);
  const command = text(argRecord.command) ?? text(data.command) ?? toolName;
  const shell = shellBadge(argRecord.shell ?? data.shell);
  const workdir = text(argRecord.workdir) ?? text(data.workdir);
  const output = text(data.output) ?? "";
  const exitCode = typeof data.exit_code === "number" ? data.exit_code : undefined;
  const truncated = data.truncated === true;
  const timedOut = data.timed_out === true;
  const status = artifact.backendStatus;
  const isActive = status === "pending" || status === "running";
  const [open, setOpen] = useToolDisclosure(status);

  return (
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      className="group/tool-call my-1 overflow-hidden rounded-xl border border-white/10 bg-zinc-900 text-zinc-100 shadow-sm"
    >
      <DisclosureRow
        leading={<TerminalIcon className="size-3.5 text-zinc-400" aria-hidden="true" />}
        label={
          <span className="flex min-w-0 items-center gap-2">
            {shell && (
              <span className="shrink-0 rounded border border-white/10 bg-white/5 px-1.5 py-px font-mono text-[10px] uppercase tracking-wide text-zinc-400">
                {shell}
              </span>
            )}
            <code className="truncate font-mono text-[13px] text-zinc-100" title={command}>
              $ {command}
            </code>
          </span>
        }
        meta={
          exitCode !== undefined ? (
            <span
              className={cn(
                "rounded px-1.5 py-px font-mono text-[11px]",
                exitCode === 0 ? "bg-emerald-500/10 text-emerald-300" : "bg-red-500/10 text-red-300",
              )}
            >
              exit {exitCode}
            </span>
          ) : null
        }
        trailing={<ToolStatus status={status} className="text-zinc-400" />}
        tone="terminal"
        className="border-b border-white/5 bg-white/[0.02] px-3"
      />
      <CollapsibleContent className="ml-6 pb-2 pl-2 pr-2">
        <div className="overflow-hidden rounded-lg border border-white/10 bg-zinc-950">
          <div className="max-h-72 overflow-auto px-3 py-2 font-mono text-xs leading-relaxed">
            {workdir && <p className="mb-1 text-[11px] text-zinc-500">cwd {workdir}</p>}
            {artifact.error && <p className="mb-1 text-red-300">{artifact.error}</p>}
            {output
              ? <pre className="whitespace-pre-wrap break-words text-zinc-300">{output}</pre>
              : !artifact.error && <p className="text-zinc-500">{isActive ? "等待输出…" : "无输出"}</p>}
            {isActive && (
              <span
                className="ml-0.5 inline-block h-3 w-1.5 animate-pulse bg-zinc-400 align-middle motion-reduce:animate-none"
                aria-hidden="true"
              />
            )}
          </div>
          {(truncated || timedOut) && (
            <p className="flex items-center gap-1.5 border-t border-amber-400/20 bg-amber-400/5 px-3 py-1.5 text-[11px] text-amber-300">
              <TriangleAlertIcon className="size-3 shrink-0" aria-hidden="true" />
              {timedOut ? "命令执行超时" : "输出已截断"}
            </p>
          )}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
