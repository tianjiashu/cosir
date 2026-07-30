/**
 * 终端命令工具卡片（execute_terminal 专用）。
 *
 * 与通用 ToolCallCard 分离：终端的折叠/展开态是独立布局——
 * 折叠行是「prompt 图标 + 命令 + 状态图标」，展开态是「命令 header + 深色输出块」。
 * 组件由 TurnTimeline 按 `display.expandLayout === "terminal"` 分发渲染，
 * 不进入 ToolCallCard 的通用分支。
 *
 * 设计要点：
 * - 命令统一来自 `args.command`；后端 `render_request_summary` 也投影同一命令，
 *   二者一致，缺失时降级为 `display.summary`。
 * - 折叠态右侧图标：运行中/成功显示绿色 Terminal，失败显示红色 AlertCircle。
 * - 展开态 header 的关闭（×）按钮仅用于折叠卡片；终止运行中命令的能力预留
 *   （暂未实现，后续可由后端任务取消 API 驱动）。
 * - 输出块只渲染命令输出（`result`）；`runData.output_truncated` 为真时
 *   额外显示截断提示，不单独展示 exit_code / timed_out 等元数据。
 *
 * @module components/chat/TerminalCallCard
 */

import { useState } from "react";
import { AlertCircle, ChevronRight, Copy, Terminal, X } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ToolDisplayInfo } from "@/services/timeline/projector";

/** 终端命令卡片状态枚举（与通用工具卡片对齐）。 */
type TerminalStatus = "running" | "completed" | "error";

/** 终端命令卡片属性。 */
interface TerminalCallCardProps {
  /** 被调用的工具名称（execute_terminal）。 */
  toolName: string;
  /** 当前执行状态。 */
  status: TerminalStatus;
  /** 待执行命令；优先来自 `args.command`。 */
  command?: string;
  /** 工具调用参数（用于兜底提取 command）。 */
  args?: Record<string, unknown>;
  /** 后端投影出的展示提示；缺省时降级为通用展示。 */
  display?: ToolDisplayInfo;
  /** 命令输出正文（展开态渲染）。 */
  result?: string | null;
  /** 失败主因。 */
  error?: string;
  /** 失败辅因。 */
  reason?: string | null;
  /** 失败是否可重试。 */
  retryable?: boolean;
  /** 执行后结构化载荷（含 output_truncated 截断标记）。 */
  resultData?: Record<string, unknown>;
}

/**
 * 从 props 中解析出要展示的命令文本。
 *
 * 优先使用 `command` 直传；缺失时回退到 `args.command`；再缺失回退到
 * `display.summary`；最终兜底为占位文案，保证折叠行始终有内容。
 *
 * 参数:
 *   command - 直传命令文本。
 *   args - 工具参数字典。
 *   display - 后端展示提示。
 *
 * 返回:
 *   人读命令字符串。
 */
function resolveCommand(
  command: string | undefined,
  args: Record<string, unknown> | undefined,
  display: ToolDisplayInfo | undefined,
): string {
  if (command && command.trim()) {
    return command;
  }
  const fromArgs = args?.command;
  if (typeof fromArgs === "string" && fromArgs.trim()) {
    return fromArgs;
  }
  if (display?.summary && display.summary.trim()) {
    return display.summary;
  }
  return "（空命令）";
}

/**
 * TerminalCallCard 终端命令卡片（可折叠）。
 *
 * 折叠态：`Terminal` prompt 图标 + 命令（超长截断）+ 右侧状态图标（成功/运行
 * 绿色 Terminal，失败红色 AlertCircle）+ 展开 chevron。
 * 展开态：命令 header（prompt 图标 + 命令 + × 关闭按钮）+ 深色等宽输出块
 * （可滚动、可复制）+ 截断提示。
 */
export function TerminalCallCard({
  status,
  command,
  args,
  display,
  result,
  error,
  reason,
  retryable,
  resultData,
}: TerminalCallCardProps) {
  const [isOpen, setIsOpen] = useState(false);
  const commandText = resolveCommand(command, args, display);
  const expandable = display?.expandable ?? true;
  // 成功/失败才允许展示输出块；运行中尚无输出。
  const hasOutput = status !== "running" && result !== null && result !== undefined;
  const isError = status === "error";

  return (
    <div className="w-full">
      {/* 折叠触发区：prompt 图标 + 命令 + 状态图标 + chevron */}
      <button
        type="button"
        onClick={() => expandable && setIsOpen((prev) => !prev)}
        className={cn(
          "flex w-full cursor-pointer items-center gap-1.5 rounded px-1 py-1 text-left text-sm hover:bg-accent/30 focus-visible:bg-accent/30 transition-colors",
          !expandable && "cursor-default",
        )}
      >
        <Terminal className="h-4 w-4 shrink-0 text-green-500" />
        <span className="min-w-0 flex-1 truncate font-mono text-xs text-foreground">
          {commandText}
        </span>
        {isError ? (
          <AlertCircle className="h-4 w-4 shrink-0 text-destructive" />
        ) : (
          <Terminal className="h-4 w-4 shrink-0 text-green-500" />
        )}
        {expandable && (
          <ChevronRight
            className={cn(
              "h-4 w-4 shrink-0 text-muted-foreground transition-transform duration-200",
              isOpen && "rotate-90",
            )}
          />
        )}
      </button>

      {/* 展开详情 */}
      {isOpen && (
        <div className="mt-1 ml-6 space-y-1.5 border-l border-border pl-3 py-1">
          {/* 命令 header：prompt 图标 + 命令 + × 关闭 */}
          <div className="flex items-center gap-1.5">
            <Terminal className="h-3.5 w-3.5 shrink-0 text-green-500" />
            <span className="min-w-0 flex-1 truncate font-mono text-xs text-foreground">
              {commandText}
            </span>
            {expandable && (
              <button
                type="button"
                onClick={() => setIsOpen(false)}
                title="折叠"
                className="shrink-0 rounded p-0.5 text-muted-foreground hover:bg-accent/40 transition-colors"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            )}
          </div>

          {/* 失败：错误主因 + 辅因 + 可重试徽标 */}
          {isError && (error || reason) && (
            <div className="space-y-1">
              {error && (
                <div className="flex gap-2">
                  <span className="text-muted-foreground shrink-0">错误:</span>
                  <span className="text-destructive">{error}</span>
                </div>
              )}
              {reason && (
                <div className="flex gap-2">
                  <span className="text-muted-foreground shrink-0">原因:</span>
                  <span className="text-muted-foreground">{reason}</span>
                </div>
              )}
              <div className="flex gap-2">
                <span className="text-muted-foreground shrink-0">重试:</span>
                <span
                  className={cn(
                    "inline-flex items-center rounded border px-1.5 py-0.5 text-[11px]",
                    retryable
                      ? "border-amber-500/40 text-amber-600"
                      : "border-border text-muted-foreground",
                  )}
                >
                  {retryable ? "可重试" : "需先修正参数"}
                </span>
              </div>
            </div>
          )}

          {/* 输出块：深色等宽 + 滚动 + 复制 */}
          {hasOutput && (
            <div className="overflow-hidden rounded border border-zinc-700">
              <div className="flex items-center gap-2 border-b border-zinc-700 bg-zinc-800 px-3 py-1.5">
                <span className="text-[11px] font-medium text-zinc-300">终端输出</span>
                <button
                  type="button"
                  onClick={() => {
                    void navigator.clipboard.writeText(result ?? "");
                  }}
                  className="ml-auto inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-zinc-300 hover:bg-zinc-700 transition-colors"
                >
                  <Copy className="h-3 w-3" />
                  复制
                </button>
              </div>
              <pre className="max-h-64 overflow-auto bg-zinc-900 p-3 font-mono text-[11px] text-zinc-100 whitespace-pre-wrap break-all">
                {result ?? ""}
              </pre>
            </div>
          )}

          {/* 输出被截断提示（不展示 exit_code / timed_out 等元数据） */}
          {hasOutput && Boolean(resultData?.output_truncated) && (
            <div className="text-[11px] text-muted-foreground">完整输出已截断</div>
          )}
        </div>
      )}
    </div>
  );
}
