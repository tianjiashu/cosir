/**
 * 可折叠工具调用卡片组件。
 *
 * 展示：
 * - 工具名称和当前状态（运行中 / 完成 / 失败）
 * - 可折叠详情区域
 *
 * @module components/chat/ToolCallCard
 */

import { useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Loader2,
  CheckCircle2,
  XCircle,
  Wrench,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";

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
}

/** 状态到视觉配置的映射。 */
const STATUS_MAP: Record<ToolCallStatus, { label: string; variant: "default" | "secondary" | "destructive" | "warning" | "outline"; Icon: React.ComponentType<{ className?: string }> }> = {
  running: { label: "运行中", variant: "warning", Icon: Loader2 },
  completed: { label: "完成", variant: "secondary", Icon: CheckCircle2 },
  error: { label: "失败", variant: "destructive", Icon: XCircle },
};

/**
 * ToolCallCard 可折叠工具调用卡片。
 *
 * 避免低价值日志淹没主会话，默认折叠展示摘要，
 * 展开后显示完整信息。
 */
export function ToolCallCard({
  toolName,
  status,
  error,
}: ToolCallCardProps) {
  const [isOpen, setIsOpen] = useState(false);
  const config = STATUS_MAP[status];
  const StatusIcon = config.Icon;

  return (
    <Collapsible open={isOpen} onOpenChange={setIsOpen}>
      <div className="rounded-md border border-border bg-card">
        {/* 折叠触发区（摘要行） */}
        <CollapsibleTrigger asChild>
          <button className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-accent/30 transition-colors">
            {isOpen ? (
              <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
            ) : (
              <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
            )}

            {/* 工具图标 */}
            <Wrench className="h-4 w-4 shrink-0 text-muted-foreground" />

            {/* 工具名称 */}
            <span className="font-medium">{toolName}</span>

            {/* 状态标签 */}
            <Badge variant={config.variant} className="ml-auto gap-1 shrink-0 text-[10px]">
              {status === "running" ? (
                <StatusIcon className="h-3 w-3 animate-spin" />
              ) : (
                <StatusIcon className="h-3 w-3" />
              )}
              {config.label}
            </Badge>
          </button>
        </CollapsibleTrigger>

        {/* 展开详情 */}
        <CollapsibleContent>
          <div className="border-t border-border px-3 py-2 space-y-1.5 text-xs">
            {/* 工具名 */}
            <div className="flex gap-2">
              <span className="text-muted-foreground shrink-0">工具:</span>
              <code className="font-mono">{toolName}</code>
            </div>

            {/* 错误信息（失败场景） */}
            {error && (
              <div className="flex gap-2">
                <span className="text-muted-foreground shrink-0">错误:</span>
                <span className="text-destructive">{error}</span>
              </div>
            )}
          </div>
        </CollapsibleContent>
      </div>
    </Collapsible>
  );
}
