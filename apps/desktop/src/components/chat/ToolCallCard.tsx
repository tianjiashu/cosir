/**
 * 工具调用展示组件。
 *
 * 折叠态模仿 Codex 风格：紧凑单行 `> 图标 动作名 参数摘要`，无边框卡片；
 * 完成后折叠行优先显示执行结果摘要（后端 result_summary_template 渲染），
 * 失败时红色高亮错误主因。展开态按成功/失败分区：成功渲染模型所见结果正文，
 * 失败渲染错误主因 + 辅因 + 可重试徽标；同时保留完整参数与打开文件动作。
 *
 * 组件不按工具名写特化分支：所有展示差异都收敛在后端 `ToolDefinition.display`，
 * 这里只做数据驱动的通用渲染。未携带 `display` 的工具降级为「工具名 + 通用参数摘要」。
 *
 * @module components/chat/ToolCallCard
 */

import type { ComponentType } from "react";
import { useState } from "react";
import {
  ChevronRight,
  Code2,
  Copy,
  ExternalLink,
  Eye,
  File,
  FilePlus,
  Folder,
  GitCompare,
  Search,
  Terminal,
  icons,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { ToolDisplayInfo } from "@/services/timeline/projector";

/** 工具调用状态枚举。 */
type ToolCallStatus = "running" | "completed" | "error";

/** 工具调用卡片属性。 */
interface ToolCallCardProps {
  /** 被调用的工具名称。 */
  toolName: string;
  /** 当前执行状态。 */
  status: ToolCallStatus;
  /** 错误主因：发生了什么（失败场景，折叠行红色高亮）。 */
  error?: string;
  /** 工具调用参数（用于展开态展示完整字典）。 */
  args?: Record<string, unknown>;
  /** 后端投影出的展示提示；缺省时降级为通用展示。 */
  display?: ToolDisplayInfo;
  /** 执行后结果摘要（成功时折叠行优先显示）。 */
  resultSummary?: string | null;
  /** 执行后完整结果正文（成功时展开态渲染）。 */
  result?: string | null;
  /** 失败辅因：为什么失败 + 如何修正（展开态补充说明）。 */
  reason?: string | null;
  /** 失败是否可重试（展开态徽标）。 */
  retryable?: boolean;
  /** 点击「打开文件」动作的回调（仅当 display.clickAction 为 open_file 时可用）。 */
  onOpenFile?: (path: string) => void;
  /** 执行后结构化载荷（通用透传），含 list 布局的 entries 与截断标记。 */
  resultData?: Record<string, unknown>;
}

/**
 * 把工具参数拼成通用降级摘要（后端未提供 display 时使用）。
 *
 * 仅做 `key=value` 拼接，不含任何工具语义特化；语义化摘要由后端 display 提供。
 *
 * 参数:
 *   args - 工具参数字典。
 *
 * 返回:
 *   人读摘要字符串；无参数时返回 null。
 */
function fallbackArgsSummary(args: Record<string, unknown> | undefined): string | null {
  if (!args || Object.keys(args).length === 0) {
    return null;
  }
  return Object.entries(args)
    .map(([key, value]) => `${key}=${JSON.stringify(value)}`)
    .join("  ");
}

/** 解析 lucide 动态图标；找不到时回退到通用扳手图标。 */
function resolveIcon(iconName: string | undefined): ComponentType<{ className?: string }> {
  const knownIcons: Record<string, ComponentType<{ className?: string }>> = {
    eye: Eye,
    file: File,
    "file-plus": FilePlus,
    folder: Folder,
    "git-compare": GitCompare,
    search: Search,
    terminal: Terminal,
  };
  if (iconName && knownIcons[iconName]) {
    return knownIcons[iconName];
  }
  if (iconName) {
    const found = (icons as Record<string, ComponentType<{ className?: string }>>)[iconName];
    if (found) {
      return found;
    }
  }
  return Eye;
}

/**
 * ToolCallCard 工具调用组件（可折叠）。
 *
 * 折叠态：紧凑单行，模仿 Codex 风格 —— `> 图标 动作名 参数/结果摘要`，
 * 无边框无背景，与对话流融为一体；失败时摘要为红色错误主因。
 * 展开态：成功显示模型结果正文（等宽、可滚动、可复制），失败显示
 * 错误主因 + 辅因 + 可重试徽标；均保留完整参数与打开文件按钮。
 */
export function ToolCallCard({
  toolName,
  status,
  error,
  args,
  display,
  resultSummary,
  result,
  reason,
  retryable,
  onOpenFile,
  resultData,
}: ToolCallCardProps) {
  const [isOpen, setIsOpen] = useState(false);
  const IconComponent = resolveIcon(display?.icon);
  // 展开态声明式信号：前端仅按布局字符串分发布局，不按工具名写特化分支。
  const expandable = display?.expandable ?? true;
  const expandLayout = display?.expandLayout ?? "details";

  // 折叠态主摘要：
  // - error    → 红色高亮错误主因（error 为主因约定）
  // - completed→ 优先执行后结果摘要，缺失时降级为执行前摘要
  // - running  → 执行前摘要（verb + summary），或「工具名 + 通用摘要」降级
  const requestSummary = display
    ? [display.verb, display.summary].filter(Boolean).join(" ")
    : [toolName, fallbackArgsSummary(args)].filter(Boolean).join(" ");
  let summaryText = requestSummary;
  if (status === "error") {
    // 失败以 error 为主因；error 空缺时降级为 reason，再兜底为固定文案，
    // 保证失败态折叠行始终有红色高亮的失败信息。
    const failureText = error || reason || "执行失败";
    summaryText = display?.verb ? `${display.verb} ${failureText}` : `${toolName} ${failureText}`;
  } else if (status === "completed" && resultSummary) {
    summaryText = display?.verb ? `${display.verb} ${resultSummary}` : resultSummary;
  }

  // 展开态参数：按 display.detailKeys 排序，再补其余参数。
  const argEntries = args ? Object.entries(args) : [];
  const orderedEntries =
    display?.detailKeys?.length && args
      ? [
          ...display.detailKeys
            .filter((key) => key in args)
            .map((key) => [key, args[key]] as [string, unknown]),
          ...argEntries.filter(([key]) => !display.detailKeys.includes(key)),
        ]
      : argEntries;

  const clickAction = display?.clickAction ?? null;
  const hasResult = result !== null && result !== undefined;
  const isChangeLayout = expandLayout === "diff" || expandLayout === "write";

  if (isChangeLayout) {
    return (
      <div className="w-full">
        <button
          type="button"
          onClick={() => expandable && setIsOpen((prev) => !prev)}
          className="flex w-full items-center gap-2 rounded border border-border bg-background px-3 py-1.5 text-left text-sm shadow-sm hover:bg-accent/30 transition-colors"
        >
          <IconComponent className="h-4 w-4 shrink-0 text-sky-600" />
          <span
            className={cn(
              "min-w-0 flex-1 truncate font-mono text-xs",
              status === "error" ? "text-destructive" : "text-foreground",
            )}
          >
            {summaryText}
          </span>
          {clickAction?.action === "open_file" && (
            <span
              role="button"
              tabIndex={0}
              onClick={(event) => {
                event.stopPropagation();
                onOpenFile?.(clickAction.target);
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  event.stopPropagation();
                  onOpenFile?.(clickAction.target);
                }
              }}
              className="shrink-0 text-xs text-sky-600 hover:underline"
            >
              查看文件
            </span>
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
        {isOpen && status === "completed" && hasResult && (
          <div className="rounded-b border-x border-b border-border bg-background px-2 py-2">
            <UnifiedDiffView content={result ?? ""} allAdded={expandLayout === "write"} />
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="w-full">
      {/* 折叠触发区：紧凑单行，模仿 Codex > 图标 动作 参数 风格 */}
      <button
        type="button"
        onClick={() => expandable && setIsOpen((prev) => !prev)}
        className="flex w-full cursor-pointer items-center gap-1.5 py-1 text-left text-sm hover:text-foreground transition-colors"
      >
        {expandable && (
          <ChevronRight
            className={cn(
              "h-4 w-4 shrink-0 text-muted-foreground transition-transform duration-200",
              isOpen && "rotate-90",
            )}
          />
        )}
        <IconComponent className="h-4 w-4 shrink-0 text-muted-foreground" />
        <span
          className={cn(
            "truncate font-mono text-xs",
            status === "error" ? "text-destructive" : "text-muted-foreground",
          )}
        >
          {summaryText}
        </span>
      </button>

      {/* 展开详情 */}
      {isOpen ? (
        <div className="ml-6 mt-1 space-y-1.5 border-l border-border pl-3 py-1 text-xs">
          {/* 工具名 */}
          <div className="flex gap-2">
            <span className="text-muted-foreground shrink-0">工具:</span>
            <code className="font-mono">{toolName}</code>
          </div>

          {/* 参数（按 detailKeys 排序优先展示） */}
          {orderedEntries.length > 0 && (
            <div className="flex gap-2">
              <span className="text-muted-foreground shrink-0">参数:</span>
              <div className="space-y-0.5">
                {orderedEntries.map(([key, value]) => (
                  <div key={key} className="font-mono text-[11px]">
                    <span className="text-muted-foreground">{key}=</span>
                    {String(JSON.stringify(value))}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* 打开文件动作（由后端 display.clickAction 驱动，数据驱动、无工具特化） */}
          {clickAction?.action === "open_file" && (
            <div className="flex gap-2">
              <span className="text-muted-foreground shrink-0">操作:</span>
              <button
                type="button"
                onClick={() => onOpenFile?.(clickAction.target)}
                className="inline-flex items-center gap-1 rounded border border-border px-1.5 py-0.5 text-[11px] hover:bg-accent/40 transition-colors"
              >
                <ExternalLink className="h-3 w-3" />
                打开文件 {clickAction.target}
              </button>
            </div>
          )}

          {/* 成功：按 expand_layout 分发差异化展开态（零工具名特化分支） */}
          {status === "completed" && hasResult && expandLayout === "list" && (
            <ListView
              entries={toListEntries(resultData?.entries)}
              emptyLabel={
                typeof resultData?.empty_label === "string" ? resultData.empty_label : undefined
              }
            />
          )}
          {status === "completed" && hasResult && expandLayout === "diff" && (
            <UnifiedDiffView content={result ?? ""} allAdded={false} />
          )}
          {status === "completed" && hasResult && expandLayout === "write" && (
            <UnifiedDiffView content={result ?? ""} allAdded />
          )}
          {status === "completed" && hasResult && expandLayout === "terminal" && (
            <TerminalBlock content={result ?? ""} />
          )}
          {status === "completed" && hasResult && expandLayout === "details" && (
            <div className="space-y-1">
              <div className="flex items-center gap-2">
                <span className="text-muted-foreground shrink-0">结果:</span>
                <button
                  type="button"
                  onClick={() => {
                    void navigator.clipboard.writeText(result ?? "");
                  }}
                  className="inline-flex items-center gap-1 rounded border border-border px-1.5 py-0.5 text-[11px] hover:bg-accent/40 transition-colors"
                >
                  <Copy className="h-3 w-3" />
                  复制
                </button>
              </div>
              <pre className="max-h-64 overflow-auto rounded bg-muted/40 p-2 font-mono text-[11px] whitespace-pre-wrap break-all">
                {result ?? ""}
              </pre>
            </div>
          )}
          {/* 受 ToolOutputBudget 裁剪时的提示（write/terminal 等全文场景） */}
          {status === "completed" && Boolean(resultData?.output_truncated) && (
            <div className="text-[11px] text-muted-foreground">
              完整输出已截断
              {resultData?.artifact_path ? `，见 ${String(resultData.artifact_path)}` : ""}
            </div>
          )}

          {/* 失败：错误主因 + 辅因 + 可重试徽标 */}
          {status === "error" && (error || reason) && (
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
        </div>
      ) : null}
    </div>
  );
}

/** list 布局的单条元素（字段形状驱动，不依赖工具名）。 */
interface ListEntry {
  name?: string;
  path?: string;
  type?: string;
  file_path?: string;
  line_number?: number;
  content?: string;
}

/** 把未知数据收窄为 ListEntry 列表（过滤非对象元素）。 */
function toListEntries(raw: unknown): ListEntry[] {
  if (!Array.isArray(raw)) return [];
  return raw.filter((e): e is ListEntry => typeof e === "object" && e !== null);
}

/**
 * list 布局：按元素字段形状渲染（不依赖工具名）。
 * 含 file_path → 搜索命中行；含 type → 目录/文件图标；含 name+path → 文件名+路径。
 */
function ListView({ entries, emptyLabel }: { entries: ListEntry[]; emptyLabel?: string }) {
  if (entries.length === 0) {
    return <div className="text-[11px] text-muted-foreground">{emptyLabel ?? "（无条目）"}</div>;
  }
  return (
    <div className="space-y-0.5">
      {entries.map((entry, idx) => {
        if (entry.file_path) {
          return (
            <div key={idx} className="flex items-baseline gap-1.5 font-mono text-[11px]">
              <Code2 className="h-3 w-3 shrink-0 text-sky-600" />
              <span className="text-muted-foreground">
                {entry.file_path}:{entry.line_number ?? 0}
              </span>{" "}
              {entry.content}
            </div>
          );
        }
        const isPython = typeof entry.name === "string" && entry.name.endsWith(".py");
        const Icon = entry.type === "dir" ? Folder : isPython ? Code2 : File;
        return (
          <div key={idx} className="flex items-center gap-1.5 font-mono text-[11px]">
            <Icon
              className={cn(
                "h-3 w-3 shrink-0",
                isPython ? "text-sky-600" : "text-muted-foreground",
              )}
            />
            <span>{entry.name}</span>
            <span className="text-muted-foreground">{entry.path}</span>
          </div>
        );
      })}
    </div>
  );
}

/**
 * diff / write 布局：按行前缀上色（+ 绿 / - 红 / @@ 蓝 / diff --git 加粗）。
 * allAdded=true（write 模式）时每行按新增绿色渲染文件全文。
 */
function UnifiedDiffView({ content, allAdded }: { content: string; allAdded?: boolean }) {
  const lines = content.split("\n");
  return (
    <pre className="max-h-64 overflow-auto rounded bg-muted/40 p-2 font-mono text-[11px] whitespace-pre-wrap break-all">
      {lines.map((line, idx) => {
        let cls = "";
        if (allAdded) {
          cls = "text-green-600";
        } else if (line.startsWith("+")) {
          cls = "text-green-600";
        } else if (line.startsWith("-")) {
          cls = "text-red-600";
        } else if (line.startsWith("@@")) {
          cls = "text-blue-500";
        } else if (line.startsWith("diff --git")) {
          cls = "font-semibold text-foreground";
        }
        return (
          <div key={idx} className={cls}>
            {line}
          </div>
        );
      })}
    </pre>
  );
}

/** terminal 布局：深色等宽 pre，可滚动 + 复制。 */
function TerminalBlock({ content }: { content: string }) {
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2">
        <span className="text-muted-foreground shrink-0">输出:</span>
        <button
          type="button"
          onClick={() => {
            void navigator.clipboard.writeText(content);
          }}
          className="inline-flex items-center gap-1 rounded border border-border px-1.5 py-0.5 text-[11px] hover:bg-accent/40 transition-colors"
        >
          <Copy className="h-3 w-3" />
          复制
        </button>
      </div>
      <pre className="max-h-64 overflow-auto rounded bg-zinc-900 p-2 font-mono text-[11px] text-zinc-100 whitespace-pre-wrap break-all">
        {content}
      </pre>
    </div>
  );
}
