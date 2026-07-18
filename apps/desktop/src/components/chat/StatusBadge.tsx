/**
 * 运行状态标签组件。
 *
 * 展示任务终态状态（已完成 / 失败 / 已取消），
 * 包含状态图标、文字描述和可选的耗时信息。
 *
 * @module components/chat/StatusBadge
 */

import { CheckCircle2, XCircle, Ban, Clock } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { RuntimeEventType } from "@shared/events";

/** 状态标签组件属性。 */
interface StatusBadgeProps {
  /** 触发展示的事件类型。 */
  eventType: RuntimeEventType;
  /** 事件载荷。 */
  payload: Record<string, unknown>;
}

/** 终态事件到视觉配置的映射。 */
const FINAL_STATUS_CONFIG: Record<string, { label: string; variant: "success" | "destructive" | "outline"; Icon: React.ComponentType<{ className?: string }> }> = {
  run_finished: { label: "任务已完成", variant: "success", Icon: CheckCircle2 },
  run_failed: { label: "任务失败", variant: "destructive", Icon: XCircle },
  run_cancelled: { label: "任务已取消", variant: "outline", Icon: Ban },
};

/**
 * StatusBadge 运行状态标签组件。
 *
 * 当收到终态事件（run_finished / run_failed / run_cancelled）时，
 * 在消息流底部渲染醒目的状态标签，帮助用户快速了解任务结果。
 */
export function StatusBadge({ eventType, payload }: StatusBadgeProps) {
  const config = FINAL_STATUS_CONFIG[eventType];

  if (!config) return null;

  const { label, variant, Icon } = config;
  const error = payload.error as string | undefined;

  return (
    <div className="flex justify-center py-2">
      <div className="flex flex-col items-center gap-2 rounded-lg border border-border bg-card px-6 py-3">
        {/* 状态图标 + 标签 */}
        <Badge variant={variant} className="gap-1.5 px-3 py-1 text-sm">
          <Icon className="h-4 w-4" />
          {label}
        </Badge>

        {/* 错误详情（如有） */}
        {error && (
          <p className="max-w-md text-center text-xs text-muted-foreground">
            {error}
          </p>
        )}

        {/* 耗时占位（后续从事件时间戳计算） */}
        <span className="flex items-center gap-1 text-[11px] text-muted-foreground">
          <Clock className="h-3 w-3" />
          耗时计算中...
        </span>
      </div>
    </div>
  );
}
