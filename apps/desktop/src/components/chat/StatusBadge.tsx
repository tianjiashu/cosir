/**
 * 运行状态标签组件。
 *
 * 当收到终态事件（run_finished / run_failed / run_cancelled / client_disconnected）
 * 时，展示任务终态状态（已完成 / 失败 / 已取消 / 连接已断开），包含状态图标、文字描述、
 * 耗时与 token 消耗信息。
 *
 * 差异化文案：run_failed 且 payload.end_reason === "client_disconnected"（或裸
 * client_disconnected 事件）表示客户端连接中断，而非 Agent 真实执行失败，组件会给出
 * 「连接已中断，内容可能未完整保存，请检查网络或重新连接后重试」的明确提示，避免用户
 * 将网络/客户端问题误判为执行失败。
 *
 * token 展示：input/output/total 任一有值即渲染 token 行；total 缺失时由前端累加
 * input+output 兜底，避免后端缓存命中场景下整块 token 信息被吞。
 *
 * @module components/chat/StatusBadge
 */

import { CheckCircle2, XCircle, Ban, Clock, Cpu, Copy, Check } from "lucide-react";
import { memo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Caption } from "@/components/ui/tokens";
import { cn } from "@/lib/utils";
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
  client_disconnected: { label: "连接已断开", variant: "destructive", Icon: XCircle },
};

/**
 * 格式化毫秒为可读耗时。
 *
 * @param ms - 毫秒数。
 * @returns 人类可读字符串；小于 1000ms 显示毫秒，否则显示秒。
 */
function formatDuration(ms: number): string {
  if (ms < 1000) {
    return `${Math.max(0, Math.round(ms))}ms`;
  }
  return `${(ms / 1000).toFixed(1)}s`;
}

/**
 * StatusBadge 运行状态标签组件。
 *
 * 当收到终态事件（run_finished / run_failed / run_cancelled）时，
 * 在消息流底部渲染醒目的状态标签，帮助用户快速了解任务结果。
 */
export const StatusBadge = memo(function StatusBadge({ eventType, payload }: StatusBadgeProps) {
  const config = FINAL_STATUS_CONFIG[eventType];

  if (!config) return null;

  const { label, variant, Icon } = config;
  const error = payload.error as string | undefined;
  const endReason = payload.end_reason as string | undefined;
  // 区分「客户端连接断开」与「真实执行失败」：前者给出明确提示与恢复建议，
  // 避免用户将网络/客户端问题误判为 Agent 执行失败。
  const isClientDisconnected = endReason === "client_disconnected" || eventType === "client_disconnected";
  const errorText = isClientDisconnected
    ? "连接已中断，本次对话可能未完整保存。请检查网络或重新连接后重试。"
    : error;

  const durationMs = typeof payload.duration_ms === "number" ? payload.duration_ms : undefined;
  const inputTokens = typeof payload.input_tokens === "number" ? payload.input_tokens : undefined;
  const outputTokens = typeof payload.output_tokens === "number" ? payload.output_tokens : undefined;
  const totalTokens = typeof payload.total_tokens === "number" ? payload.total_tokens : undefined;
  const langfuseTraceId = typeof payload.langfuse_trace_id === "string" ? payload.langfuse_trace_id : undefined;

  return (
    <div className="flex justify-center py-2">
      <div className="flex flex-col items-center gap-2 rounded-lg border border-border bg-card px-6 py-3">
        {/* 状态图标 + 标签 */}
        <Badge variant={variant} className="gap-1.5 px-3 py-1 text-sm">
          <Icon className="h-4 w-4" />
          {label}
        </Badge>

        {/* 错误详情（如有） */}
        {errorText && (
          <p className="max-w-md text-center text-xs text-muted-foreground">
            {errorText}
          </p>
        )}

        {/* 耗时 */}
        {durationMs !== undefined && (
          <span className={cn("flex items-center gap-1 text-muted-foreground", Caption.xs)}>
            <Clock className="h-3 w-3" />
            耗时 {formatDuration(durationMs)}
          </span>
        )}

        {/* Token 消耗：后端可能只下发 input/output（如缓存命中场景缺失 total），
            因此任一字段有值即展示，total 缺失时由前端累加 input+output 兜底。 */}
        {(inputTokens !== undefined || outputTokens !== undefined || totalTokens !== undefined) && (
          <span className={cn("flex items-center gap-1 text-muted-foreground", Caption.xs)}>
            <Cpu className="h-3 w-3" />
            输入 {inputTokens ?? 0} / 输出 {outputTokens ?? 0} / 总计{" "}
            {totalTokens ?? (inputTokens ?? 0) + (outputTokens ?? 0)} tokens
          </span>
        )}

        {/* Langfuse Trace ID */}
        {langfuseTraceId && (
          <CopyableTraceId traceId={langfuseTraceId} />
        )}
      </div>
    </div>
  );
});

/** 可复制展示的 Langfuse trace ID。 */
function CopyableTraceId({ traceId }: { traceId: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(traceId);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // 复制失败静默处理，避免打断 UI
    }
  };

  return (
    <button
      type="button"
      onClick={handleCopy}
      className={cn("flex max-w-xs items-center gap-1.5 rounded bg-muted px-2 py-1 text-muted-foreground transition-colors hover:text-foreground", Caption.xs)}
      title="复制 Langfuse trace ID"
    >
      <span className="truncate font-mono">Langfuse trace: {traceId}</span>
      {copied ? (
        <Check className="h-3 w-3 shrink-0 text-green-500" />
      ) : (
        <Copy className="h-3 w-3 shrink-0" />
      )}
    </button>
  );
}
