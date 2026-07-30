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

import type { ComponentType, SyntheticEvent } from "react";
import { useMemo, useState } from "react";
import {
  AlertCircle,
  ChevronRight,
  Code2,
  Columns2,
  Copy,
  ExternalLink,
  Eye,
  File,
  FilePlus,
  Folder,
  GitCompare,
  Rows3,
  Search,
  Terminal,
  icons,
} from "lucide-react";
import { Diff, parseDiff } from "react-diff-view";
import type { FileData } from "react-diff-view";
import "react-diff-view/style/index.css";
import { cn } from "@/lib/utils";
import type { ToolDisplayInfo } from "@/services/timeline/projector";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

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
  // diff/write 两态共享的视图模式；提升到这里避免两个分支重复声明 hook。
  const [diffViewType, setDiffViewType] = useState<"unified" | "split">("unified");
  const IconComponent = resolveIcon(display?.icon);
  // 错误态使用圆圈内叹号图标，与成功/运行态的工具图标做视觉区分。
  const StatusIcon = status === "error" ? AlertCircle : IconComponent;
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
    // diff/write 布局共享的视图模式与解析结果；折叠态文件头需要和展开态一致。
    const diffFiles = useMemo<FileData[]>(() => {
      if (!result) return [];
      try {
        return parseDiff(result);
      } catch {
        return [];
      }
    }, [result]);
    const firstFile = diffFiles[0];

    return (
      <TooltipProvider>
        <div className="w-full">
          <div
            role="button"
            tabIndex={0}
            onClick={() => expandable && setIsOpen((prev) => !prev)}
            onKeyDown={(event) => {
              if ((event.key === "Enter" || event.key === " ") && expandable) {
                event.preventDefault();
                setIsOpen((prev) => !prev);
              }
            }}
            className="flex w-full cursor-pointer items-center gap-1.5 rounded px-1 py-1 text-left text-sm hover:bg-accent/30 focus-visible:bg-accent/30 hover:text-foreground transition-colors"
          >
            {status === "completed" && firstFile ? (
              <>
                <DiffFileHeaderContent
                  file={firstFile}
                  content={result ?? ""}
                  openPath={clickAction?.target}
                  viewType={diffViewType}
                  setViewType={setDiffViewType}
                  onOpenFile={onOpenFile}
                  stopPropagation
                />
                {expandable && (
                  <ChevronRight
                    className={cn(
                      "h-4 w-4 shrink-0 text-muted-foreground transition-transform duration-200",
                      isOpen && "rotate-90",
                    )}
                  />
                )}
              </>
            ) : (
              <>
                <StatusIcon className="h-4 w-4 shrink-0 text-muted-foreground" />
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span
                      className={cn(
                        "min-w-0 flex-1 truncate font-mono text-xs",
                        status === "error" ? "text-muted-foreground" : "text-foreground",
                      )}
                    >
                      {shortenChangeSummary(summaryText)}
                    </span>
                  </TooltipTrigger>
                  <TooltipContent className="max-w-xs break-all font-mono text-[11px]">
                    {summaryText}
                  </TooltipContent>
                </Tooltip>
                {expandable && status !== "error" && (
                  <ChevronRight
                    className={cn(
                      "h-4 w-4 shrink-0 text-muted-foreground transition-transform duration-200",
                      isOpen && "rotate-90",
                    )}
                  />
                )}
              </>
            )}
          </div>
          {isOpen && status === "completed" && hasResult && (
            <div className="px-2 pb-2">
              <FileDiffView content={result ?? ""} viewType={diffViewType} />
            </div>
          )}
        </div>
      </TooltipProvider>
    );
  }

  return (
    <div className="w-full">
      {/* 折叠触发区：紧凑单行，模仿 Codex > 图标 动作 参数 风格 */}
      <button
        type="button"
        onClick={() => expandable && setIsOpen((prev) => !prev)}
        className="flex w-full cursor-pointer items-center gap-1.5 rounded px-1 py-1 text-left text-sm hover:bg-accent/30 focus-visible:bg-accent/30 hover:text-foreground transition-colors"
      >
        {expandable && status !== "error" && (
          <ChevronRight
            className={cn(
              "h-4 w-4 shrink-0 text-muted-foreground transition-transform duration-200",
              isOpen && "rotate-90",
            )}
          />
        )}
        <StatusIcon className="h-4 w-4 shrink-0 text-muted-foreground" />
        <span
          className={cn(
            "truncate font-mono text-xs",
            status === "error" ? "text-muted-foreground" : "text-muted-foreground",
          )}
        >
          {summaryText}
        </span>
      </button>

      {/* 展开详情 */}
      {isOpen ? (
        <div className="ml-6 mt-1 space-y-1.5 border-l border-border pl-3 py-1 text-xs">
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
            <FileDiffView content={result ?? ""} viewType={diffViewType} />
          )}
          {status === "completed" && hasResult && expandLayout === "write" && (
            <FileDiffView content={result ?? ""} viewType={diffViewType} />
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
 * 基于 react-diff-view 的 GitHub 风格 diff 渲染。
 *
 * 直接消费后端生成的 unified diff 文本（parseDiff），无需反向解析为旧/新版；
 * 每个文件独立渲染文件头（basename + 完整路径 tooltip + 变更统计 + 复制/打开/分栏切换），
 * 主体由 react-diff-view 的 `Diff` 组件按行号列 + 红绿底色渲染。
 * 解析失败（非标准 diff 文本）时回退到 `UnifiedDiffView` 纯文本渲染，避免空白块。
 *
 * 参数:
 *   content - 后端生成的 unified diff 文本（diff --git a/... b/... 开头）。
 *   onOpenFile - 点击「在编辑器打开」回调，传入归一化后的文件路径。
 *   openPath - 优先使用的打开路径（通常来自 display.clickAction.target）。
 *
 * 返回:
 *   React 渲染节点。
 *
 * @throws 不抛出异常；parseDiff 异常被捕获并降级为纯文本渲染。
 *
 * @sideeffect 无。
 */
function FileDiffView({
  content,
  viewType,
}: {
  content: string;
  viewType: "unified" | "split";
}) {
  const files = useMemo<FileData[]>(() => {
    try {
      return parseDiff(content);
    } catch {
      return [];
    }
  }, [content]);

  // 解析失败（纯文本 / 非标准 diff）回退到行染色的纯文本渲染，避免空白块。
  if (files.length === 0) {
    return <UnifiedDiffView content={content} />;
  }

  return (
    <div className="space-y-3">
      {files.map((file, fileIdx) => (
        <div
          key={`${file.newPath}-${fileIdx}`}
          className="overflow-hidden rounded border border-border bg-background"
        >
          {/* diff 主体：react-diff-view 渲染，自带行号列与红绿底色；字体与折叠态对齐为 11px */}
          <div className="max-h-[480px] overflow-auto text-[11px]">
            <Diff diffType={file.type} hunks={file.hunks} viewType={viewType} />
          </div>
        </div>
      ))}
    </div>
  );
}

/**
 * diff 文件头：图标 + basename + 完整路径 tooltip + 变更统计 + 复制/打开/视图切换。
 * 供折叠态与展开态共享，保证两态视觉一致。
 *
 * 参数:
 *   file        - react-diff-view 解析出的文件数据。
 *   content     - 原始 diff 文本，用于复制与路径兜底提取。
 *   openPath    - 优先使用的打开路径（通常来自 display.clickAction.target）。
 *   viewType    - 当前 diff 视图模式。
 *   setViewType - 切换视图模式回调。
 *   onOpenFile  - 点击「在编辑器打开」回调。
 *   stopPropagation - 操作按钮是否阻止事件冒泡（折叠态使用，避免触发展开/折叠）。
 *
 * 返回:
 *   React 渲染节点。
 *
 * @sideeffect 操作按钮可能写入剪贴板或调用 onOpenFile。
 */
function DiffFileHeaderContent({
  file,
  content,
  openPath,
  viewType,
  setViewType,
  onOpenFile,
  stopPropagation = false,
}: {
  file: FileData;
  content: string;
  openPath?: string;
  viewType: "unified" | "split";
  setViewType: (value: "unified" | "split") => void;
  onOpenFile?: (path: string) => void;
  stopPropagation?: boolean;
}) {
  const rawPath =
    file.newPath || file.oldPath || openPath || extractFallbackPath(content) || "未知文件";
  const normalized = normalizeDiffPath(rawPath);
  const isRename = file.type === "rename" && file.oldPath !== file.newPath;
  const fullPath = isRename
    ? `${normalizeDiffPath(file.oldPath)} → ${normalized}`
    : normalized;
  const { added, removed } = countDiffChanges(file);
  const openTarget = openPath && openPath.length > 0 ? openPath : normalized;
  const isNewFile = file.type === "add";

  const wrapHandler =
    <E extends SyntheticEvent>(handler?: (event: E) => void) =>
    (event: E) => {
      if (stopPropagation) {
        event.stopPropagation();
      }
      handler?.(event);
    };

  return (
    <div className="flex w-full flex-1 items-center gap-1.5">
      <GitCompare className="h-4 w-4 shrink-0 text-muted-foreground" />
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="min-w-0 flex-1 truncate font-mono text-xs text-foreground">
            {isNewFile ? "新建 " : ""}
            {basenameOf(rawPath)}
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs break-all font-mono text-[11px]">
          {fullPath}
        </TooltipContent>
      </Tooltip>
      {added > 0 && (
        <Badge
          variant="outline"
          className="shrink-0 border-green-200 px-1.5 py-0 text-[11px] font-medium tabular-nums text-green-700"
        >
          +{added}
        </Badge>
      )}
      {removed > 0 && (
        <Badge
          variant="outline"
          className="shrink-0 border-red-200 px-1.5 py-0 text-[11px] font-medium tabular-nums text-red-700"
        >
          -{removed}
        </Badge>
      )}
      <div className="ml-auto flex shrink-0 items-center gap-0.5">
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="h-7 w-7"
          title="复制 diff"
          onClick={wrapHandler(() => {
            void navigator.clipboard.writeText(content);
          })}
        >
          <Copy className="h-3.5 w-3.5" />
        </Button>
        {onOpenFile && (
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            title="在编辑器打开"
            onClick={wrapHandler(() => onOpenFile(openTarget))}
          >
            <ExternalLink className="h-3.5 w-3.5" />
          </Button>
        )}
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="h-7 w-7"
          title={viewType === "unified" ? "切换为分栏视图" : "切换为单栏视图"}
          onClick={wrapHandler(() => setViewType(viewType === "unified" ? "split" : "unified"))}
        >
          {viewType === "unified" ? (
            <Columns2 className="h-3.5 w-3.5" />
          ) : (
            <Rows3 className="h-3.5 w-3.5" />
          )}
        </Button>
      </div>
    </div>
  );
}

/** 去掉 unified diff 路径前缀 a/ b/。 */
function normalizeDiffPath(raw: string): string {
  return raw.replace(/^[ab]\//, "");
}

/** 取路径最后一段作为文件名展示。 */
function basenameOf(raw: string): string {
  const normalized = normalizeDiffPath(raw);
  const segments = normalized.split("/");
  return segments[segments.length - 1] || normalized;
}

/** 把 diff/write 折叠态的长路径摘要截断为 basename；非路径文本原样保留。 */
function shortenChangeSummary(text: string): string {
  if (text.includes("/")) {
    return basenameOf(text);
  }
  return text;
}

/**
 * 从原始 diff 文本里兜底提取文件路径（parseDiff 未解析出路径时使用）。
 * 依次尝试 `diff --git a/x b/y`、`+++ path`、`--- path` 三种行格式。
 *
 * 参数:
 *   content - 原始 unified diff 文本。
 *
 * 返回:
 *   提取到的路径字符串；未找到时返回 null。
 */
function extractFallbackPath(content: string): string | null {
  const lines = content.split("\n");
  for (const line of lines) {
    const gitMatch = line.match(/^diff --git a\/(.+?) b\/(.+)$/);
    if (gitMatch) return gitMatch[2];
    const plusMatch = line.match(/^\+\+\+ (.+)$/);
    if (plusMatch) return plusMatch[1];
    const minusMatch = line.match(/^--- (.+)$/);
    if (minusMatch) return minusMatch[1];
  }
  return null;
}

/** 统计单文件新增 / 删除行数。 */
function countDiffChanges(file: FileData): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const hunk of file.hunks) {
    for (const change of hunk.changes) {
      if (change.type === "insert") {
        added += 1;
      } else if (change.type === "delete") {
        removed += 1;
      }
    }
  }
  return { added, removed };
}

/**
 * diff / write 布局：按行前缀上色（+ 绿 / - 红 / @@ 蓝 / diff --git 加粗）。
 * 作为 `FileDiffView` 解析失败时的纯文本回退渲染使用。
 */
function UnifiedDiffView({ content }: { content: string }) {
  const lines = content.split("\n");
  return (
    <pre className="max-h-[480px] overflow-auto rounded bg-muted/40 p-2 font-mono text-[11px] whitespace-pre-wrap break-all">
      {lines.map((line, idx) => {
        let cls = "";
        if (line.startsWith("+")) {
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
