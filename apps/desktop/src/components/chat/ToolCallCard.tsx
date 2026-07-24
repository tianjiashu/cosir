/**
 * 工具调用展示组件。
 *
 * 折叠态模仿 Codex 风格：紧凑单行 `> 图标 动作名 参数摘要`，无边框卡片；
 * 展开态显示完整参数、错误信息等详情。
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
  ExternalLink,
  Eye,
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
  /** 错误信息（失败场景）。 */
  error?: string;
  /** 工具调用参数（用于展开态展示完整字典）。 */
  args?: Record<string, unknown>;
  /** 后端投影出的展示提示；缺省时降级为通用展示。 */
  display?: ToolDisplayInfo;
  /** 点击「打开文件」动作的回调（仅当 display.clickAction 为 open_file 时可用）。 */
  onOpenFile?: (path: string) => void;
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
 * 折叠态：紧凑单行，模仿 Codex 风格 —— `> 图标 动作名 参数摘要`，
 * 无边框无背景，与对话流融为一体。
 * 展开态：显示完整参数、打开文件按钮、错误信息等。
 */
export function ToolCallCard({ toolName, status, error, args, display, onOpenFile }: ToolCallCardProps) {
  const [isOpen, setIsOpen] = useState(false);
  const IconComponent = resolveIcon(display?.icon);

  // 折叠态主摘要：优先用后端语义摘要（verb + summary），否则降级为「工具名 + 通用摘要」。
  const summaryText = display
    ? [display.verb, display.summary].filter(Boolean).join(" ")
    : [toolName, fallbackArgsSummary(args)].filter(Boolean).join(" ");

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

  // 状态指示小圆点颜色
  const statusColor =
    status === "running"
      ? "text-amber-500"
      : status === "completed"
        ? "text-green-500"
        : "text-red-500";

  return (
    <div className="w-full">
      {/* 折叠触发区：紧凑单行，模仿 Codex > 图标 动作 参数 风格 */}
      <button
        type="button"
        onClick={() => setIsOpen((prev) => !prev)}
        className="flex w-full cursor-pointer items-center gap-1.5 py-1 text-left text-sm hover:text-foreground transition-colors"
      >
        <ChevronRight
          className={cn(
            "h-4 w-4 shrink-0 text-muted-foreground transition-transform duration-200",
            isOpen && "rotate-90",
          )}
        />
        <IconComponent className="h-4 w-4 shrink-0 text-muted-foreground" />
        <span className="truncate font-mono text-xs text-muted-foreground">{summaryText}</span>
        {/* 状态小圆点 */}
        <span className={cn("ml-auto h-1.5 w-1.5 shrink-0 rounded-full", statusColor)} />
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

          {/* 错误信息（失败场景） */}
          {error && (
            <div className="flex gap-2">
              <span className="text-muted-foreground shrink-0">错误:</span>
              <span className="text-destructive">{error}</span>
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
