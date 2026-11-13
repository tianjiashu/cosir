import { Maximize2Icon } from "lucide-react";
import { useRef } from "react";

import { useClipped } from "@/hooks/use-clipped";
import { callIsExecuting } from "@/lib/assistant/tools/call-status";
import { useOpenLink } from "@/components/links/use-open-link";
import { CopyIconButton } from "@/components/ui/copy-icon-button";
import { REVEAL_ON_DISCLOSURE_HOVER } from "@/components/ui/reveal-on-hover";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { useDisclosure } from "@/lib/assistant/turn/disclosure-store";
import { OmittedOutputNotice } from "../terminal-output/omitted-notice";
import { TerminalOutputPreview, useParsedOutput } from "../terminal-output/preview";
import { TerminalOutputReader } from "../terminal-output/reader";
import { resolvePathText } from "@/lib/links/resolve-link";
import { stopTarget } from "@/lib/assistant/command-request-store";
import { cancelToolCall } from "@/lib/assistant/task-commands";
import type { CallView, CallViewContext } from "../call-view";
import { readText as text } from "../display-data-readers";
import { useCallRanAWhile } from "../call-elapsed";
import { ForegroundWaitAction } from "../foreground-wait-action";
import { ToolStopButton, useStopRequest } from "../tool-stop-button";

/** 输出按原样保留：纯空白也是输出，不走 `readText`。 */
function rawOutput(data: Record<string, unknown>): string {
  return typeof data.output === "string" ? data.output : "";
}

function exitCode(data: Record<string, unknown>): number | undefined {
  return typeof data.exit_code === "number" ? data.exit_code : undefined;
}

/** 卡片输出只保留最近一段；被省略的行数与完整日志路径由后端展示数据给出。 */
function omittedLines(data: Record<string, unknown>): number | undefined {
  return typeof data.output_omitted_lines === "number" ? data.output_omitted_lines : undefined;
}

function outputLogPath(data: Record<string, unknown>): string | undefined {
  return typeof data.output_log_path === "string" ? data.output_log_path : undefined;
}

/** 命令自己结束时的结局文案；结局事实是后端的 `end`，前端不从退出码或工具状态反推。 */
function endedLabel(data: Record<string, unknown>): string {
  if (data.end === "timed_out") return "命令超时";
  if (data.end === "signaled") return `被信号 ${String(data.signal)} 结束`;
  const code = exitCode(data);
  return code ? `非零退出 ${code}` : "已结束";
}

/**
 * 「命令还活着」：转到后台后占位是 `pending`，但输出仍在追加，展开体要跟着它而不是工具状态。
 * `background` 是所有后台结果共用的事实（part 级），不读 `display_data`。
 */
function commandLive({ artifact, workStatus, inherited }: CallViewContext): boolean {
  return !inherited && callIsExecuting(artifact.backendStatus, artifact.background, workStatus);
}

/** 这次命令此刻的状态文案：卡片底部状态条与阅读器共用，事实只读工具状态与后端的 `end`。 */
function terminalStatusLabel(data: Record<string, unknown>, context: CallViewContext): string {
  const { artifact } = context;
  if (context.inherited && (artifact.backendStatus === "pending" || artifact.backendStatus === "running")) return "继承时未结束";
  if (commandLive(context)) return context.taskCancelling ? "正在停止…" : "运行中";
  if (context.workStatus === "settling") return "后台结果收尾中";
  if (artifact.backendStatus === "cancelled") return "已取消";
  if (data.end === "timed_out") return "命令超时";
  if (artifact.backendStatus === "failed") return "执行失败";
  return endedLabel(data);
}

/**
 * 去留操作只给可交互面上仍在前台运行的命令：转到后台的命令在看板外壳上停止，只读面（子 Agent 阅读器、
 * 看板详情）与 Task 停止中都不给。
 */
function liveActionsAvailable(context: CallViewContext): boolean {
  return context.interactive && !context.taskCancelling && !context.artifact.background && commandLive(context);
}

/**
 * 前台命令只停这一条（随 Task 停止也会一并结束）。图标按钮与子 Agent、Team 的停止同一个样子，次于「转到后台」：
 * 它会中断命令，用得少。HTTP 只确认进程内信号已发出，「正在停止」保持到 snapshot 里的工具状态落定。
 */
function TerminalStopButton({ taskId, toolCallId }: CallViewContext) {
  const cancellation = useStopRequest(taskId, stopTarget(toolCallId));
  return <ToolStopButton label="终端命令" pending={cancellation.pending}
    onClick={() => void cancellation.stop(() => cancelToolCall(taskId, toolCallId))} />;
}

/**
 * 去留操作跑满与调用行时长同一门槛才出现，很快结束的命令不闪按钮。单独成组件，让每秒计时只重渲染这一小段。
 */
function TerminalLiveActions(context: CallViewContext) {
  if (!useCallRanAWhile(context.artifact.startedAt)) return null;
  return <>
    <ForegroundWaitAction taskId={context.taskId} toolCallId={context.toolCallId} />
    <TerminalStopButton {...context} />
  </>;
}

/**
 * 底部状态条：左边是此刻的状态，前台运行中右边给去留操作。放在输出下面是因为等命令时视线停在输出末尾，
 * 「还要不要等」是看着输出决定的；结束后同一位置换成结局，固定最小高度让操作出现、消失时卡片不跳。
 * 没有输出时不另画占位，由这里说「运行中」。
 */
function TerminalStatusBar({ data, context }: { data: Record<string, unknown>; context: CallViewContext }) {
  return (
    <div data-slot="terminal-status-bar" className="text-muted-foreground flex min-h-6 items-center gap-1.5">
      {commandLive(context) && <span aria-hidden="true" className="bg-muted-foreground/70 size-1.5 shrink-0 rounded-full motion-safe:animate-pulse" />}
      <span className="min-w-0 flex-1 truncate">{terminalStatusLabel(data, context)}</span>
      {liveActionsAvailable(context) && <TerminalLiveActions {...context} />}
    </div>
  );
}

/**
 * 终端卡片：命令框 · 输出尾部 · 底部状态条，按时间往下读。命令从启动起就在（后端启动时即给出展示数据）；
 * 状态不决定已收集输出是否可读。复制命令常显，输出的复制与放大悬停才出现。
 */
function TerminalResultBody({ data, context }: { data: Record<string, unknown>; context: CallViewContext; }) {
  const { taskId, toolCallId } = context;
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
  const commandClipped = useClipped(commandElement, !commandOpen, [command]);
  return (
    <div data-slot="terminal-card" className="group/disclosure bg-card space-y-2 rounded-xl border border-border/60 px-3 pt-2.5 pb-1.5 text-xs leading-relaxed">
      {command && <div>
        <div className="flex items-start gap-2 rounded-lg border border-border/60 px-2.5 py-1.5 font-mono">
          <span className="text-muted-foreground select-none" aria-hidden="true">$</span>
          <pre ref={commandElement} data-terminal-command="true" className={`min-w-0 flex-1 font-mono break-all whitespace-pre-wrap ${commandOpen ? "" : "line-clamp-2"}`}>{command}</pre>
          <CopyIconButton label="复制命令" size="xs" className="-my-0.5" getText={() => command} logEvent="terminal_command_copy_failed" logData={{ taskId, toolCallId }} />
        </div>
        {(commandOpen || commandClipped) && <Button variant="ghost" size="sm" onClick={() => setCommandOpen(!commandOpen)} aria-expanded={commandOpen}>{commandOpen ? "收起命令" : "显示完整命令"}</Button>}
      </div>}
      {output ? <div className="relative">
        <OmittedOutputNotice omittedLines={omittedLines(data)} logPath={outputLogPath(data)} links={{ workspaceRoot: links.workspaceRoot, open: links.open }} />
        <div className={cn("bg-card absolute top-0 right-0 flex gap-1", REVEAL_ON_DISCLOSURE_HOVER)}>
          <CopyIconButton label="复制输出" size="xs" getText={() => parsed?.text ?? ""} logEvent="terminal_output_copy_failed" logData={{ taskId, toolCallId }} />
          <TooltipIconButton tooltip="查看输出" className="size-6" onClick={() => setReaderOpen(true)}><Maximize2Icon aria-hidden="true" /></TooltipIconButton>
        </div>
        {parsed && parsed.hiddenLines > 0 && <p className="text-muted-foreground">还有 {parsed.hiddenLines} 行</p>}
        {parsed && <TerminalOutputPreview lines={parsed.lines} />}
      </div> : !live && <p className="text-muted-foreground">无输出</p>}
      {showWorkdir && <p className="text-muted-foreground truncate text-2xs" title={workdir}>
        启动目录：{directoryTarget
          ? <button type="button" className="hover:text-foreground underline-offset-2 hover:underline" onClick={() => links.open(directoryTarget)} aria-label={`打开启动目录：${directoryName}`}>{directoryName}</button>
          : directoryName}
      </p>}
      <TerminalStatusBar data={data} context={context} />
    </div>
  );
}

/** 独立阅读器不继承过程/调用详情的可见性，关闭详情仍接收最新输出。 */
function TerminalReaderOverlay({ data, context }: { data: Record<string, unknown>; context: CallViewContext; }) {
  const { taskId, toolCallId } = context;
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
    omittedLines={omittedLines(data)} logPath={outputLogPath(data)}
    links={{ getWorkspaceRoot: () => links.workspaceRoot, onOpenLink: links.open }} open={open} onOpenChange={setOpen}
    command={text(data.command) ?? text(context.args.command)}
    statusLabel={terminalStatusLabel(data, context)} /></>;
}

/**
 * `terminal-result`（`execute_terminal`）：标题是模型写的意图（`heading.title`），命令只在详情显示，这里只负责
 * 结果——元信息只在退出码非 0 时给中性的 `exit N`：探测命令的非零退出是常态，不当警告；`exit 0` 与成功同义不显示。
 * 被信号结束给 `signal N`；超时、取消、没能启动、结局未知没有退出码，由工具状态与错误短提示表达。
 * 行上没有操作：转到后台与停止在卡片底部状态条，看着输出再决定。后台命令的输出、阅读器与停止都在看板，时间线由
 * `resolveCallView` 收成一行。
 */
export const terminalResultView: CallView = (data, context) => {
  const code = exitCode(data);
  const meta = data.end === "signaled" ? `signal ${String(data.signal)}` : code ? `exit ${code}` : undefined;
  return {
    meta,
    overlay: <TerminalReaderOverlay data={data} context={context} />,
    detail: { render: () => <TerminalResultBody data={data} context={context} /> },
  };
};
