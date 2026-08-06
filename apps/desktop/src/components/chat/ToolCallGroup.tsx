/**
 * 同 turn 内连续工具调用的聚合展示组件。
 *
 * 把一组相邻的工具调用（通常一次 Agent 行动会连续调用多个工具）收成一行
 * 「⚙ 运行了 N 个工具 ▾」摘要，展开后才逐条渲染各工具明细卡片。
 * 该组件只做「外层折叠 + 内层复用」，自身不渲染任何工具语义内容；
 * 所有单工具展示差异（摘要文本、diff/list 布局、terminal 渲染）仍由各明细卡片数据驱动完成。
 *
 * 聚合边界由上层（TurnTimeline 的分组 pass）决定：仅「连续 tool 段」入组，
 * 中间穿插的 assistant / thinking / status 会打断聚合，保证流式期组大小单调增长、不抖动。
 *
 * @module components/chat/ToolCallGroup
 */

import { memo, useState } from "react";
import { AlertCircle, ChevronRight, Wrench } from "lucide-react";
import { cn } from "@/lib/utils";
import { ToolCallCard } from "@/components/chat/ToolCallCard";
import { TerminalCallCard } from "@/components/chat/TerminalCallCard";
import type { TimelineToolItem } from "@/services/timeline/projector";

/** 工具调用聚合组属性。 */
interface ToolCallGroupProps {
  /** 组唯一标识：基于首项 callId + 组内工具数，保证流式期引用稳定。 */
  groupId: string;
  /** 组内按原始顺序排列的工具项（长度 ≥ 2）。 */
  items: TimelineToolItem[];
  /** 点击「打开文件」动作的回调（透传给各明细卡片）。 */
  onOpenFile?: (path: string) => void;
}

/**
 * 聚合行：把同段连续工具调用收成一行摘要。
 *
 * 折叠态：`⚙ 运行了 N 个工具` + 右侧「M 成功 / K 失败」小字 + 展开箭头；
 * 若存在 running 态工具，显示「正在运行 N 个工具…」并带 pulse 动效。
 * 展开态：按原始顺序逐条渲染明细卡片——terminal 布局走 TerminalCallCard，
 * 其余走 ToolCallCard（复用既有明细组件）；组内每卡保持各自的折叠态，不随组展开而被强制展开。
 *
 * @param props.groupId  - 组唯一标识。
 * @param props.items    - 组内工具项数组（长度 ≥ 2）。
 * @param props.onOpenFile - 打开文件回调。
 */
export const ToolCallGroup = memo(function ToolCallGroup({
  groupId,
  items,
  onOpenFile,
}: ToolCallGroupProps) {
  const [isOpen, setIsOpen] = useState(false);

  const total = items.length;
  const running = items.filter((item) => item.status === "running").length;
  const failed = items.filter((item) => item.status === "error").length;
  const succeeded = total - running - failed;
  const hasRunning = running > 0;

  // 折叠态右侧小字统计：失败优先显示，其次运行/成功，避免信息过载。
  const stats = [
    hasRunning ? `${running} 进行中` : null,
    failed > 0 ? `${failed} 失败` : null,
    succeeded > 0 && !hasRunning ? `${succeeded} 成功` : null,
  ].filter(Boolean) as string[];

  return (
    <div className="w-full min-w-0">
      {/* 聚合折叠行：低视觉权重，无背景无边框，与单工具卡折叠行风格一致 */}
      <button
        type="button"
        onClick={() => setIsOpen((prev) => !prev)}
        className="flex w-full cursor-pointer items-center gap-1.5 rounded px-1 py-1 text-left text-sm hover:bg-accent/30 focus-visible:bg-accent/30 transition-colors"
      >
        <ChevronRight
          className={cn(
            "h-4 w-4 shrink-0 text-muted-foreground transition-transform duration-200",
            isOpen && "rotate-90",
          )}
        />
        {failed > 0 ? (
          <AlertCircle className="h-4 w-4 shrink-0 text-destructive" />
        ) : (
          <Wrench
            className={cn(
              "h-4 w-4 shrink-0 text-muted-foreground",
              hasRunning && "animate-pulse",
            )}
          />
        )}
        <span className="truncate text-xs text-muted-foreground">
          {hasRunning ? `正在运行 ${total} 个工具…` : `运行了 ${total} 个工具`}
        </span>
        {stats.length > 0 && (
          <span className="ml-auto shrink-0 text-xs text-muted-foreground/70">
            {stats.join(" · ")}
          </span>
        )}
      </button>

      {/* 展开明细：逐条复用明细卡片，保持各自折叠态 */}
      {isOpen && (
        <div className="ml-6 mt-1 space-y-0.5 border-l border-border/60 pl-3 py-1">
          {items.map((item, idx) => {
            const key = item.callId ?? item.eventId ?? `${groupId}-${idx}`;
            // terminal 布局工具复用独立 TerminalCallCard；其余走通用 ToolCallCard。
            if (item.display?.expandLayout === "terminal") {
              return (
                <TerminalCallCard
                  key={key}
                  toolName={item.toolName}
                  status={item.status}
                  command={item.arguments?.command as string | undefined}
                  args={item.arguments}
                  display={item.display}
                  result={item.result}
                  output={item.output}
                  error={item.error}
                  reason={item.reason}
                  retryable={item.retryable}
                  resultData={item.resultData}
                />
              );
            }
            if (item.status === "running") {
              return (
                <ToolCallCard
                  key={key}
                  toolName={item.toolName}
                  status="running"
                  args={item.arguments}
                  display={item.display}
                  requestSummary={item.requestSummary}
                  resultData={item.resultData}
                  onOpenFile={onOpenFile}
                />
              );
            }
            return (
              <ToolCallCard
                key={key}
                toolName={item.toolName}
                status={item.status}
                error={item.error}
                args={item.arguments}
                display={item.display}
                resultSummary={item.resultSummary}
                result={item.result}
                reason={item.reason}
                retryable={item.retryable}
                requestSummary={item.requestSummary}
                listEntries={item.listEntries}
                emptyLabel={item.emptyLabel}
                resultData={item.resultData}
                onOpenFile={onOpenFile}
              />
            );
          })}
        </div>
      )}
    </div>
  );
});
