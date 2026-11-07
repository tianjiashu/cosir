import { Maximize2Icon } from "lucide-react";
import { useLayoutEffect, useRef, useState } from "react";

import { callIsExecuting } from "@/lib/assistant/call-status";
import { useOpenLink } from "@/components/links/use-open-link";
import { CopyIconButton } from "@/components/ui/copy-icon-button";
import { REVEAL_ON_HOVER_CLASS } from "@/components/ui/disclosure-chevron";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { useDisclosure } from "@/lib/assistant/disclosure-store";
import { TerminalOutputPreview, useParsedOutput } from "../terminal-output-preview";
import { TerminalOutputReader } from "../terminal-output-reader";
import { resolvePathText } from "@/lib/links/resolve-link";
import { stopTarget } from "@/lib/assistant/command-request-store";
import { cancelToolCall } from "@/lib/assistant/task-commands";
import type { CallView, CallViewContext } from "../call-view";
import { DetailSurface } from "../details/detail-view";
import { readText as text } from "../display-utils";
import { ToolStopButton, useStopRequest } from "../tool-stop-button";

/** 输出按原样保留：纯空白也是输出，不走 `readText`。 */
function rawOutput(data: Record<string, unknown>): string {
  return typeof data.output === "string" ? data.output : "";
}

function exitCode(data: Record<string, unknown>): number | undefined {
  return typeof data.exit_code === "number" ? data.exit_code : undefined;
}

/**
 * 「命令还活着」：转到后台后占位是 `pending`，但输出仍在追加，展开体要跟着它而不是工具状态。
 * `background` 是所有后台结果共用的事实（part 级），不读 `display_data`。
 */
function commandLive({ artifact, workStatus, inherited }: CallViewContext): boolean {
  return !inherited && callIsExecuting(artifact.backendStatus, artifact.background, workStatus);
}

/** 单份命令、有限尾部预览与独立阅读器；状态不决定已收集输出是否可读。操作悬停才出现，卡片平时只有内容。 */
function TerminalResultBody({ data, context }: { data: Record<string, unknown>; context: CallViewContext; }) {
  const { artifact, taskId, toolCallId } = context;
  const command = text(data.command) ?? text(context.args.command);
  const output = rawOutput(data);
  const workdir = text(data.workdir);
  const live = commandLive(context);
  const links = useOpenLink("tool", taskId);
  const directoryTarget = workdir ? resolvePathText(`${workdir}/`, { workspaceRoot: links.workspaceRoot }) : null;
  const directoryName = workdir?.replace(/[\\/]+$/, "").split(/[\\/]/).at(-1) || workdir;
  // 在工作区根目录跑命令是常态，只有换了目录才值得说。
  const showWorkdir = workdir !== undefined && workdir.replace(/[\\/]+$/, "") !== links.workspaceRoot?.replace(/[\\/]+$/, "");
  const parsed = useParsedOutput(output);
  const [commandOpen, setCommandOpen] = useDisclosure(taskId, `terminal-command:${toolCallId}`, false);
  const [, setReaderOpen] = useDisclosure(taskId, `terminal-reader:${toolCallId}`, false);
  const commandElement = useRef<HTMLPreElement>(null);
  const [commandClipped, setCommandClipped] = useState(false);
  // 隐藏时量到的是 0，重新显示时观察者会再触发一次。
  useLayoutEffect(() => {
    const element = commandElement.current;
    if (!element || commandOpen) return;
    const measure = () => setCommandClipped(element.scrollHeight > element.clientHeight + 1);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [command, commandOpen]);
  return (
    <DetailSurface className="group/disclosure space-y-2 leading-relaxed">
      {command && <div>
        <div className="flex items-start gap-2 font-mono">
          <span className="text-muted-foreground select-none" aria-hidden="true">$</span>
          <pre ref={commandElement} data-terminal-command="true" className={`min-w-0 flex-1 font-mono break-all whitespace-pre-wrap ${commandOpen ? "" : "line-clamp-2"}`}>{command}</pre>
          <CopyIconButton label="复制命令" size="xs" className={REVEAL_ON_HOVER_CLASS} getText={() => command} logEvent="terminal_command_copy_failed" logData={{ taskId, toolCallId }} />
        </div>
        {(commandOpen || commandClipped) && <Button variant="ghost" size="sm" onClick={() => setCommandOpen(!commandOpen)} aria-expanded={commandOpen}>{commandOpen ? "收起命令" : "显示完整命令"}</Button>}
      </div>}
      {output ? <div className="relative border-t pt-2">
        <div className={cn("bg-surface absolute top-1 right-0 flex gap-1", REVEAL_ON_HOVER_CLASS)}>
          <CopyIconButton label="复制输出" size="xs" getText={() => parsed?.text ?? ""} logEvent="terminal_output_copy_failed" logData={{ taskId, toolCallId }} />
          <TooltipIconButton tooltip="查看输出" className="size-6" onClick={() => setReaderOpen(true)}><Maximize2Icon aria-hidden="true" /></TooltipIconButton>
        </div>
        {parsed && parsed.hiddenLines > 0 && <p className="text-muted-foreground">还有 {parsed.hiddenLines} 行</p>}
        {parsed && <TerminalOutputPreview lines={parsed.lines} />}
      </div> : <p className="text-muted-foreground" data-terminal-background={artifact.background ? (live ? "running" : "ended") : undefined}>{live ? "等待输出…" : "无输出"}</p>}
      {showWorkdir && <p className="text-muted-foreground truncate text-2xs" title={workdir}>
        启动目录：{directoryTarget
          ? <button type="button" className="hover:text-foreground underline-offset-2 hover:underline" onClick={() => links.open(directoryTarget)} aria-label={`打开启动目录：${directoryName}`}>{directoryName}</button>
          : directoryName}
      </p>}
    </DetailSurface>
  );
}

/** 独立阅读器不继承过程/调用详情的可见性，关闭详情仍接收最新输出。 */
function TerminalReaderOverlay({ data, context }: { data: Record<string, unknown>; context: CallViewContext; }) {
  const { artifact, taskId, toolCallId } = context;
  const [open, setOpen] = useDisclosure(taskId, `terminal-reader:${toolCallId}`, false);
  const links = useOpenLink("tool", taskId);
  const anchor = useRef<HTMLSpanElement>(null);
  // 原打开按钮随过程收起后仍连接 DOM；把焦点交回最近的可见标题。看板里没有调用行，也不会随过程收起。
  const finalFocus = () => {
    const row = anchor.current?.closest<HTMLElement>("[data-tool-call-id]");
    if (!row || row.checkVisibility()) return true;
    const groupHead = row.closest('[data-slot="activity-group"]')?.querySelector<HTMLElement>('[data-slot="activity-head"]');
    if (groupHead?.checkVisibility()) return groupHead;
    const processHead = row.closest('[data-slot="turn"]')?.querySelector<HTMLElement>('[data-slot="process-head"]');
    return processHead?.checkVisibility() ? processHead : false;
  };
  return <><span ref={anchor} hidden /><TerminalOutputReader finalFocus={finalFocus} taskId={taskId} toolCallId={toolCallId} output={rawOutput(data)}
    links={{ getWorkspaceRoot: () => links.workspaceRoot, onOpenLink: links.open }} open={open} onOpenChange={setOpen}
    command={text(data.command) ?? text(context.args.command)}
    statusLabel={context.inherited && (artifact.backendStatus === "pending" || artifact.backendStatus === "running") ? "继承时未结束" : commandLive(context) ? "运行中" : context.workStatus === "settling" ? "后台结果收尾中" : artifact.backendStatus === "cancelled" ? "已取消" : data.timed_out === true ? "命令超时" : artifact.backendStatus === "failed" ? "执行失败" : exitCode(data) ? `非零退出 ${exitCode(data)}` : "已结束"} /></>;
}

/**
 * 行尾的停止，只给前台命令（随 Task 停止一并结束）；后台命令的停止在看板外壳上。
 * HTTP 只确认进程内信号已发出，「停止中」保持到 snapshot 里的工具状态落定。
 */
function TerminalStopAction({ taskId, toolCallId }: CallViewContext) {
  const cancellation = useStopRequest(taskId, stopTarget(toolCallId));
  const request = () => cancellation.stop(() => cancelToolCall(taskId, toolCallId));
  return <ToolStopButton label="终端命令" pending={cancellation.pending} onClick={() => void request()} />;
}

/**
 * `terminal-result`（`execute_terminal`）：标题是模型写的意图（`heading.title`），命令只在详情显示，这里只负责
 * 结果——元信息只在退出码非 0 时给中性的 `exit N`：探测命令的非零退出是常态，不当警告；`exit 0` 与成功同义不显示。
 * 后台命令的输出、阅读器与停止都在看板，时间线由 `resolveCallView` 收成一行。
 */
export const terminalResultView: CallView = (data, context) => {
  const code = exitCode(data);
  const meta = code ? `exit ${code}` : undefined;
  const canCancel = context.interactive && !context.taskCancelling && commandLive(context);
  return {
    meta,
    overlay: <TerminalReaderOverlay data={data} context={context} />,
    actions: canCancel ? <TerminalStopAction {...context} /> : null,
    detail: { render: () => <TerminalResultBody data={data} context={context} /> },
  };
};
