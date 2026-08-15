/**
 * 终端命令工具卡片（execute_terminal 专用）。
 *
 * 与通用 ToolCallCard 分离：终端的折叠/展开态是独立布局——
 * 折叠行是「浅灰卡片 + prompt 图标 + 完整命令 + 状态/展开操作」，
 * 展开态是「命令 header + 浅色等宽输出块」。
 * 组件由 TurnTimeline 按 `display.expandLayout === "terminal"` 分发渲染，
 * 不进入 ToolCallCard 的通用分支。
 *
 * 设计要点：
 * - 命令统一来自 `args.command`；缺失时降级为占位文案。
 * - 折叠态：多行命令经 `whitespace-pre-wrap` 保留真实换行，限制最大高度避免长命令撑破卡片，
 *   右侧常驻展开 chevron，不截断命令内容。
 * - 展开态 header：独立浅色背景与卡片主体隔离，右侧提供「复制命令」「状态」「关闭」三个操作；
 *   命令区使用 `Caption.mono`（11px 等宽 token）而非裸 `text-xs`，折行后左侧缩进与图标对齐。
 * - 输出块分运行态与终态两条互斥路径：运行中由 `TerminalViewer`（xterm）渲染实时流，
 *   终态切回浅色等宽 `<pre>` 渲染最终输出；两者都支持一键复制。
 * - 运行期首次收到实时输出会自动展开卡片；用户手动折叠后不再强制展开。
 * - `resultData.output_truncated` 为真时额外显示截断提示，不单独展示 exit_code / timed_out 等元数据。
 * - header 的关闭（×）按钮仅用于折叠卡片；终止运行中命令的能力预留
 *   （暂未实现，后续可由后端任务取消 API 驱动）。
 *
 * @module components/chat/TerminalCallCard
 */

import * as React from "react";
import { memo } from "react";
import { AlertCircle, Check, ChevronDown, Copy, Terminal, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { Caption } from "@/components/ui/tokens";
import { logError } from "@/lib/logger";
import { TerminalViewer } from "@/components/chat/TerminalViewer";
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
  /** 命令输出正文（终态展开渲染）。 */
  result?: string | null;
  /** 运行期实时累积输出（仅 `status === "running"` 时用 xterm 渲染）。 */
  output?: string;
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
 * 优先使用 `command` 直传；缺失时回退到 `args.command`；最终兜底为占位文案，
 * 保证折叠行始终有内容。后端不再产出命令摘要文本，命令仅来自参数。
 *
 * 参数:
 *   command - 直传命令文本。
 *   args - 工具参数字典。
 *   display - 后端展示提示（仅用于静态声明，不承载命令文本）。
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
  // display 仅含静态声明（verb/icon/expandable/expandLayout），无命令文本，直接兜底。
  void display;
  return "（空命令）";
}

/**
 * 复制按钮（图标 + 复制成功反馈）。
 *
 * 复制成功后临时展示绿色 Check 图标，2 秒后恢复，与项目内 CodeBlock/StatusBadge 保持一致。
 *
 * @param props - 组件属性。
 * @param props.text - 要复制到剪贴板的文本。
 * @param props.className - 额外样式类。
 * @param props.title - 按钮 hover 提示。
 * @param props.children - 默认显示的图标（可选）。
 */
function CopyButton({
  text,
  className,
  title = "复制",
  children,
}: {
  text: string;
  className?: string;
  title?: string;
  children?: React.ReactNode;
}) {
  const [copied, setCopied] = React.useState(false);

  const handleCopy = React.useCallback(
    async (event: React.MouseEvent) => {
      event.stopPropagation();
      try {
        await navigator.clipboard.writeText(text);
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      } catch (err) {
        // 剪贴板写入失败时经统一日志出口记录，不静默吞错，便于排查环境权限问题。
        logError("复制命令到剪贴板失败", err, { module: "TerminalCallCard" });
      }
    },
    [text],
  );

  return (
    <button
      type="button"
      onClick={handleCopy}
      title={title}
      className={cn(
        "inline-flex h-6 w-6 shrink-0 items-center justify-center rounded text-muted-foreground hover:bg-accent/40 hover:text-foreground transition-colors",
        className,
      )}
    >
      {copied ? <Check className="h-3.5 w-3.5 text-emerald-500" /> : children ?? <Copy className="h-3.5 w-3.5" />}
    </button>
  );
}

/**
 * TerminalCallCard 终端命令卡片（可折叠）。
 *
 * 折叠态：浅灰圆角卡片，左侧 prompt 图标，中间命令原文换行展示，
 * 右侧状态图标 + 展开 chevron。
 * 展开态：命令 header（prompt 图标 + 命令 + 复制/状态/关闭操作）+ 浅色等宽输出块
 * （可滚动、可复制）+ 截断提示。
 */
export const TerminalCallCard = memo(function TerminalCallCard({
  status,
  command,
  args,
  display,
  result,
  output,
  error,
  reason,
  retryable,
  resultData,
}: TerminalCallCardProps) {
  const [isOpen, setIsOpen] = React.useState(false);
  /** 用户是否手动折叠过：一旦为真，运行期不再自动展开，避免覆盖用户意图。 */
  const userCollapsedRef = React.useRef(false);
  const commandText = resolveCommand(command, args, display);
  const expandable = display?.expandable ?? true;
  const isError = status === "error";
  const isRunning = status === "running";
  // 运行期空终端也应立即出现：只要进入 running 即挂载 xterm 视图（无需等待首个 delta），
  // 命令启动到产出首行输出之间的空窗期也呈现「正在执行」的终端质感。
  const hasStreamingOutput = isRunning;
  // 运行中展示实时流；终态展示最终输出正文。
  const hasOutput = isRunning ? hasStreamingOutput : result !== null && result !== undefined;

  const toggleOpen = React.useCallback(() => {
    if (!expandable) {
      return;
    }
    setIsOpen((prev) => {
      if (prev) {
        userCollapsedRef.current = true;
      }
      return !prev;
    });
  }, [expandable]);

  const collapse = React.useCallback(() => {
    userCollapsedRef.current = true;
    setIsOpen(false);
  }, []);

  // 进入运行期即自动展开，让空白终端立即可见、无需手动点击；
  // 命令启动到产出首行输出之间的空窗期也呈现「正在执行」的终端质感。
  // 用户手动折叠过则尊重其选择，不再强制展开。
  React.useEffect(() => {
    if (expandable && isRunning && !userCollapsedRef.current) {
      setIsOpen(true);
    }
  }, [expandable, isRunning]);

  return (
    <div className="w-full overflow-hidden rounded-md border border-border bg-muted">
      {/* Header：折叠态即整个命令行卡片，展开态复用同一 header；展开时加深背景与主体隔离 */}
      <div
        className={cn(
          "flex items-start gap-2 px-2.5 py-2",
          isOpen && "bg-muted-foreground/5",
          expandable && !isOpen && "cursor-pointer hover:bg-accent/20",
        )}
        onClick={expandable && !isOpen ? toggleOpen : undefined}
        role={expandable && !isOpen ? "button" : undefined}
        tabIndex={expandable && !isOpen ? 0 : undefined}
        onKeyDown={(event) => {
          if (expandable && !isOpen && (event.key === "Enter" || event.key === " ")) {
            event.preventDefault();
            toggleOpen();
          }
        }}
      >
        <Terminal
          className={cn(
            "mt-0.5 h-3.5 w-3.5 shrink-0",
            isError ? "text-destructive" : "text-green-600",
          )}
        />
        <div className="min-w-0 flex-1">
          {/* 折叠态保留真实换行并限高，避免长命令被单行截断看不到；
              展开态命令折行后左侧留白与图标对齐，使用 Caption.mono（11px 等宽 token） */}
          <span
            className={cn(
              Caption.mono,
              "block text-foreground",
              isOpen
                ? "whitespace-pre-wrap break-words"
                : "max-h-12 overflow-hidden whitespace-pre-wrap",
            )}
          >
            {commandText}
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-0.5">
          {isOpen && <CopyButton text={commandText} title="复制命令" />}

          {/* 状态图标 */}
          {isError ? (
            <div className="flex h-6 w-6 items-center justify-center text-destructive" title="执行失败">
              <AlertCircle className="h-4 w-4" />
            </div>
          ) : isRunning ? (
            <div className="flex h-6 w-6 items-center justify-center text-green-600" title="运行中">
              <Terminal className="h-4 w-4" />
            </div>
          ) : (
            <div className="flex h-6 w-6 items-center justify-center text-green-600" title="执行成功">
              <Terminal className="h-4 w-4" />
            </div>
          )}

          {expandable && !isOpen && (
            <button
              type="button"
              onClick={(event) => {
                event.stopPropagation();
                setIsOpen(true);
              }}
              title="展开"
              className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded text-muted-foreground hover:bg-accent/40 hover:text-foreground transition-colors"
            >
              <ChevronDown className="h-4 w-4" />
            </button>
          )}

          {isOpen && (
            <button
              type="button"
              onClick={(event) => {
                event.stopPropagation();
                collapse();
              }}
              title="关闭"
              className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded text-muted-foreground hover:bg-accent/40 hover:text-foreground transition-colors"
            >
              <X className="h-4 w-4" />
            </button>
          )}
        </div>
      </div>

      {/* 展开详情 */}
      {isOpen && (
        <div className="space-y-2 px-3 pb-3 pt-1">
          {/* 失败：错误主因 + 辅因 + 可重试徽标 */}
          {isError && (error || reason) && (
            <div className="space-y-1 text-sm">
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
                    "inline-flex items-center rounded border px-1.5 py-0.5",
                    Caption.xs,
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

          {/* 输出块：运行中走 xterm 实时流，终态走静态 <pre>（两者互斥，避免双渲染） */}
          {hasOutput && (
            <div className="overflow-hidden rounded border border-border bg-background">
              <div className="flex items-center gap-2 border-b border-border bg-muted-foreground/5 px-3 py-1.5">
                <span className={cn(Caption.xs, "font-medium text-muted-foreground")}>
                  {isRunning ? "终端输出（运行中）" : "终端输出"}
                </span>
                <CopyButton text={(isRunning ? output : result) ?? ""} title="复制输出" className="ml-auto" />
              </div>
              {isRunning ? (
                <TerminalViewer output={output ?? ""} className="p-2" />
              ) : (
                <pre className={cn("max-h-64 overflow-auto p-3 text-foreground whitespace-pre-wrap break-words", Caption.mono)}>
                  {result ?? ""}
                </pre>
              )}
            </div>
          )}

          {/* 输出被截断提示（不展示 exit_code / timed_out 等元数据） */}
          {hasOutput && !isRunning && Boolean(resultData?.output_truncated) && (
            <div className={cn(Caption.xs, "text-muted-foreground")}>完整输出已截断</div>
          )}
        </div>
      )}
    </div>
  );
});
