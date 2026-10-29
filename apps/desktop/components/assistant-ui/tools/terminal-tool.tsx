import { LoaderCircleIcon, TriangleAlertIcon } from "lucide-react";
import type { ReactNode } from "react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { Badge } from "@/components/ui/badge";
import { Collapsible, CollapsibleContent } from "@/components/ui/collapsible";
import { DisclosureRow } from "../elements/disclosure-row.aui";
import { asRecord, readToolArtifact } from "./types";
import { ToolStatus } from "./tool-status";
import { useToolDisclosure } from "./tool-disclosure";
import { ToolStopButton, useStopRequest } from "./tool-stop-button";
import { cancelToolCall } from "@/lib/assistant/task-commands";
import { cn } from "@/lib/utils";
import { TerminalViewport } from "./terminal-viewport";
import { TERMINAL_CARD_CLASS, TERMINAL_CARD_HEADER_CLASS } from "../elements/tool-layout-tokens";

/** 显式 shell 的标注文案；`auto` 沿用宿主默认 shell，不标注。 */
const SHELL_LABELS: Record<string, string> = {
  cmd: "cmd",
  powershell: "powershell",
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
 * 把后端 shell 值映射为标注文案。
 *
 * `auto` 与缺失都表示宿主默认 shell，返回 null 以避免无信息量的标注。
 */
function shellLabel(value: unknown): string | null {
  return typeof value === "string" ? SHELL_LABELS[value] ?? null : null;
}

type TerminalToolProps = ToolCallMessagePartProps & {
  /** 工具取消按 Task 寻址：后端命中 Task 当前执行中的这次调用。 */
  taskId: number;
  /** 只读展示面（如后台看板里的同一张卡片）不提供停止，停止入口由所在面统一提供。 */
  interactive?: boolean;
  taskCancelling?: boolean;
};

/**
 * 以嵌入式深色终端卡片渲染 execute_terminal 调用。
 *
 * 命令与 shell 优先取自工具参数：参数在 running 态即随状态事件下发，早于子进程
 * 真正执行，因此命令能在执行前就渲染出来。运行期间输出来自有界 Transport snapshot
 * 增量；最终输出与退出码由 terminal 终态 display_data 收口。
 *
 * 表头只保留一个结论位：退出码非 0 时以 `exit N` 代替状态图标（工具状态仍是 completed，
 * 但对用户而言命令失败了），`exit 0` 与成功对勾重复，不显示。cwd、截断、超时等辅助信息
 * 收进输出下方的状态栏，不混进输出正文。
 *
 * 命令转到后台时卡片是后台占位：part 的 `deferred` 为真（所有延迟结果共用的事实，不读
 * `display_data`）、状态为 `pending`，表头带「后台」标注（收起时也可见）。后台阶段输出照常实时
 * 追加，结论并入时完整输出与退出码写回卡片，所以与前台一样渲染输出；还没有输出时给一句后台状态。
 * 停止仍按 `toolCallId` 取消（后端撤销登记并杀进程树）。
 */
export function TerminalTool({ toolName, toolCallId, args, artifact: rawArtifact, taskId, interactive = true, taskCancelling = false }: TerminalToolProps) {
  const artifact = readToolArtifact(rawArtifact);
  const data = artifact.display_data ?? {};
  const argRecord = asRecord(args);
  const command = text(argRecord.command) ?? text(data.command) ?? toolName;
  const shell = shellLabel(argRecord.shell ?? data.shell);
  const workdir = text(argRecord.workdir) ?? text(data.workdir);
  const output = text(data.output) ?? "";
  const exitCode = typeof data.exit_code === "number" ? data.exit_code : undefined;
  const truncated = data.truncated === true;
  const streamTruncated = data.stream_truncated === true;
  const timedOut = data.timed_out === true;
  const inBackground = artifact.deferred;
  const status = artifact.backendStatus;
  const isActive = status === "pending" || status === "running";
  const commandFailed = status === "failed" || (exitCode !== undefined && exitCode !== 0);
  // 后台占位不随 Task 停止，取消按钮只看它自己的状态；前台运行中的命令随 Task 停止一并结束。
  const canCancel = toolName === "execute_terminal"
    && interactive
    && (inBackground ? status === "pending" : status === "running" && !taskCancelling);
  // 展开跟随「命令还活着」而不是工具状态：转到后台后占位是 pending，但输出仍在追加，收起会藏住它。
  const commandLive = status === "running" || (inBackground && status === "pending");
  const [open, setOpen] = useToolDisclosure(commandLive ? "running" : status);
  const cancellation = useStopRequest(isActive);

  const requestCancellation = async () => {
    if (!canCancel) return;
    const stopped = await cancellation.stop(async () => {
      const result = await cancelToolCall(taskId, toolCallId);
      // HTTP 只确认进程内信号已发出；「停止中」保持到 snapshot 里的工具状态落定。
      return result.kind === "signalled" || result.kind === "already_signalled" ? null : result.message;
    });
    if (!stopped) setOpen(true);
  };

  const warning = timedOut
    ? "命令执行超时"
    : streamTruncated && isActive
      ? "实时输出已达到展示上限"
      : truncated || streamTruncated ? "输出已截断" : null;
  const footerItems: ReactNode[] = [];
  if (workdir) {
    footerItems.push(<span key="cwd" className="min-w-0 truncate font-mono" title={workdir}>{workdir}</span>);
  }
  if (warning) {
    footerItems.push(
      <span key="warning" className="text-warning ml-auto flex shrink-0 items-center gap-1">
        <TriangleAlertIcon className="size-3" aria-hidden="true" />
        {warning}
      </span>,
    );
  }

  return (
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      className={cn("group/tool-call", TERMINAL_CARD_CLASS, commandFailed && "border-destructive/40")}
      data-terminal-failed={commandFailed || undefined}
    >
      {commandFailed && <span aria-hidden="true" className="bg-destructive absolute inset-y-0 left-0 z-10 w-0.5" />}
      <div className={TERMINAL_CARD_HEADER_CLASS}>
        <DisclosureRow
          leading={<span className="text-success font-mono text-xs font-semibold" aria-hidden="true">$</span>}
          label={
            <code className="text-foreground block truncate font-mono text-xs" title={command}>
              {command}
            </code>
          }
          meta={
            <span className="flex items-center gap-1.5">
              {shell && <span className="text-muted-foreground font-mono text-2xs">{shell}</span>}
              {inBackground && (
                <Badge tone="info" variant="outline" shape="tag" size="sm">后台</Badge>
              )}
            </span>
          }
          trailing={<TerminalOutcome status={status} exitCode={exitCode} inBackground={inBackground} />}
          tone="terminal"
          className="w-auto min-w-0 flex-1 bg-transparent px-3"
        />
        {canCancel && (
          <ToolStopButton label="终端命令" pending={cancellation.pending} onClick={() => void requestCancellation()} />
        )}
      </div>
      <CollapsibleContent>
        <div className="bg-terminal-inset border-t">
          <div className="px-3 py-2 font-mono text-xs leading-relaxed">
            {cancellation.error && <p className="text-warning mb-1" role="alert">{cancellation.error}</p>}
            {artifact.error
              ? <p className="text-destructive">{artifact.error}</p>
              : output
                ? <TerminalViewport output={output} />
                : inBackground
                  ? (
                    <p className="text-muted-foreground" data-terminal-background={isActive ? "running" : "ended"}>
                      {isActive ? "命令在后台运行，输出会实时显示在这里" : "命令已在后台结束，没有输出"}
                    </p>
                  )
                  : <p className="text-muted-foreground">{isActive ? "等待输出…" : "无输出"}</p>}
          </div>
          {footerItems.length > 0 && (
            <div className="text-muted-foreground flex min-w-0 items-center gap-3 border-t px-3 py-1 text-2xs">
              {footerItems}
            </div>
          )}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

/**
 * 表头的唯一结论位：非 0 退出码优先于工具状态；后台运行中的占位状态是 `pending`，
 * 通用文案「准备执行」会误导，改为后台运行提示。
 */
function TerminalOutcome({ status, exitCode, inBackground }: {
  status: ReturnType<typeof readToolArtifact>["backendStatus"];
  exitCode: number | undefined;
  inBackground: boolean;
}) {
  if (exitCode !== undefined && exitCode !== 0) {
    return (
      <Badge tone="destructive" variant="outline" shape="tag" size="sm" className="font-mono">
        exit {exitCode}
      </Badge>
    );
  }
  if (inBackground && status === "pending") {
    return (
      <span className="text-info inline-flex items-center gap-1.5 text-xs">
        <LoaderCircleIcon className="size-3.5 animate-spin" aria-hidden="true" />
        后台运行中
      </span>
    );
  }
  return <ToolStatus status={status} />;
}
