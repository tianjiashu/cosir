import { useRef, useState } from "react";

import { useOpenLink } from "@/components/links/use-open-link";
import { CopyIconButton } from "@/components/ui/copy-icon-button";
import { cancelToolCall } from "@/lib/assistant/task-commands";
import { notify } from "@/lib/notify";
import type { CallView, CallViewContext } from "../call-view";
import { TerminalViewport, type TerminalViewportHandle } from "../terminal-viewport";
import { ToolStopButton, useStopRequest } from "../tool-stop-button";

function text(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function exitCode(data: Record<string, unknown>): number | undefined {
  return typeof data.exit_code === "number" ? data.exit_code : undefined;
}

/**
 * 「命令还活着」：转到后台后占位是 `pending`，但输出仍在追加，展开体要跟着它而不是工具状态。
 * `background` 是所有后台结果共用的事实（part 级），不读 `display_data`。
 */
function commandLive({ artifact }: CallViewContext): boolean {
  return artifact.backendStatus === "running" || (artifact.background && artifact.backendStatus === "pending");
}

/** 悬停或键盘聚焦到展开体时才出现的小按钮：复制是低频操作，不常驻。 */
const HOVER_ACTION_CLASS = "size-5 shrink-0 opacity-0 transition-opacity group-hover/terminal-body:opacity-100 group-focus-within/terminal-body:opacity-100";

/**
 * 展开体：命令行（悬停可复制命令与输出）、丢行提示、输出视窗、工作目录。复制与链接都是只读操作，后台
 * 看板里（`interactive=false`）照常提供。
 */
function TerminalResultBody({ data, context }: { data: Record<string, unknown>; context: CallViewContext }) {
  const { artifact, taskId, toolCallId } = context;
  const command = text(data.command) ?? text(context.args.command);
  const output = text(data.output) ?? "";
  const workdir = text(data.workdir);
  const live = commandLive(context);
  const links = useOpenLink("tool", taskId);
  const viewportRef = useRef<TerminalViewportHandle | null>(null);
  const [truncated, setTruncated] = useState(false);
  return (
    <div className="group/terminal-body bg-surface rounded-lg px-3 py-2 font-mono text-xs leading-relaxed">
      {command && (
        <div className="flex min-w-0 items-center gap-1">
          <p className="text-muted-foreground min-w-0 flex-1 truncate" title={command}>$ {command}</p>
          <CopyIconButton
            label="复制命令"
            className={HOVER_ACTION_CLASS}
            getText={() => command}
            logEvent="terminal_command_copy_failed"
            logData={{ taskId, toolCallId }}
          />
          {output && (
            <CopyIconButton
              label="复制输出"
              className={HOVER_ACTION_CLASS}
              getText={() => viewportRef.current?.copyText() ?? ""}
              logEvent="terminal_output_copy_failed"
              logData={{ taskId, toolCallId }}
            />
          )}
        </div>
      )}
      {output && truncated && <p className="text-muted-foreground text-2xs">较早的输出未显示</p>}
      {output
        ? (
          <TerminalViewport
            ref={viewportRef}
            output={output}
            links={{ getWorkspaceRoot: () => links.workspaceRoot, onOpenLink: (target) => links.open(target) }}
            onTruncatedChange={setTruncated}
          />
        )
        : (
          <p className="text-muted-foreground" data-terminal-background={artifact.background ? (live ? "running" : "ended") : undefined}>
            {artifact.background
              ? live ? "命令在后台运行，输出会实时显示在这里" : "命令已在后台结束，没有输出"
              : live ? "等待输出…" : "无输出"}
          </p>
        )}
      {(workdir || data.timed_out === true) && (
        <p className="text-muted-foreground mt-1 flex min-w-0 items-center gap-3 text-2xs">
          {workdir && <span className="min-w-0 truncate" title={workdir}>{workdir}</span>}
          {data.timed_out === true && <span className="text-warning ml-auto shrink-0">命令执行超时</span>}
        </p>
      )}
    </div>
  );
}

/**
 * 行尾的停止：后台占位不随 Task 停止，取消只看它自己的状态；前台运行中的命令随 Task 停止一并结束。
 * HTTP 只确认进程内信号已发出，「停止中」保持到 snapshot 里的工具状态落定。
 */
function TerminalStopAction({ taskId, toolCallId, artifact }: CallViewContext) {
  const active = artifact.backendStatus === "pending" || artifact.backendStatus === "running";
  const cancellation = useStopRequest(active);
  const request = () => cancellation.stop(async () => {
    const result = await cancelToolCall(taskId, toolCallId);
    if (result.kind === "signalled" || result.kind === "already_signalled") return null;
    notify.error(result.message);
    return result.message;
  });
  return <ToolStopButton label="终端命令" pending={cancellation.pending} onClick={() => void request()} />;
}

/**
 * `terminal-result`（`execute_terminal`）：标题是模型写的意图（`heading.title`）、目标是命令本身，这里只负责
 * 结果——元信息只在退出码非 0 时给 `exit N`（工具状态仍是 completed，但对用户而言命令失败了），`exit 0`
 * 与成功同义不显示；展开体是输出视窗，运行中默认展开、结束即收起、退出码非 0 时默认带出输出尾部。
 */
export const terminalResultView: CallView = {
  meta: (data) => {
    const code = exitCode(data);
    if (code !== undefined && code !== 0) return <span className="text-destructive font-mono">exit {code}</span>;
    if (data.timed_out === true) return <span className="text-warning">超时</span>;
    return null;
  },
  body: (data, context) => <TerminalResultBody data={data} context={context} />,
  defaultOpen: (data, context) => commandLive(context) || (exitCode(data) ?? 0) !== 0,
  actions: (_data, context) => {
    const { artifact, interactive, taskCancelling } = context;
    const canCancel = interactive
      && (artifact.background ? artifact.backendStatus === "pending" : artifact.backendStatus === "running" && !taskCancelling);
    return canCancel ? <TerminalStopAction {...context} /> : null;
  },
};
