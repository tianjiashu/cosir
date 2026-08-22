/**
 * 运行状态标签组件。
 *
 * 当收到终态事件（run_finished / run_failed / run_cancelled）时，展示任务终态状态
 * （已完成 / 失败 / 已取消），包含状态图标、文字描述、耗时与 token 消耗信息。
 *
 * 差异化文案：run_failed 时按 payload.end_reason 区分失败性质（两者都不是独立
 * event_type，FINAL_STATUS_CONFIG 中无对应 key，差异化完全由 payload.end_reason 驱动）：
 *   - "client_disconnected"：客户端连接中断（后端经 finally 兜底标记 turn failed 下发
 *     run_failed 并在 payload 中携带该 end_reason），而非 Agent 真实执行失败，给出
 *     「连接已中断，内容可能未完整保存，请检查网络或重新连接后重试」的明确提示，避免
 *     将网络/客户端问题误判为执行失败。
 *   - "max_steps_reached"：Agent 达到最大步骤数但未产出最终回答（model 节点统一收口），
 *     给出「已达到最大步骤数，未产出最终回答」的说明，避免用户只看到 max_steps_reached
 *     枚举码而无法理解停止原因。
 *
 * token 展示：input/output/total 任一有值即渲染 token 行；total 缺失时由前端累加
 * input+output 兜底，避免后端缓存命中场景下整块 token 信息被吞。
 *
 * @module components/chat/StatusBadge
 */

import { CheckCircle2, XCircle, Ban, Clock, Cpu, Copy, Check } from "lucide-react";
import { memo } from "react";
import { Badge } from "@/components/ui/badge";
import { Caption } from "@/components/ui/tokens";
import { cn } from "@/lib/utils";
import { useCopyToClipboard } from "@/hooks/useCopyToClipboard";
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
 * 解析 run_failed 事件的错误展示文本。
 *
 * 按 payload.end_reason 区分失败性质给出可读文案（区别于原生 error 枚举码）：
 *   - "client_disconnected"：客户端连接中断，非 Agent 真实执行失败，给出恢复建议；
 *   - "max_steps_reached"：达到最大步骤数未产出最终回答，说明停止原因；
 *   - 其余情况原样返回 payload.error。
 *
 * @param payload - run_failed 事件载荷（读取 error 与 end_reason）。
 * @returns 展示用错误文本；error 缺失时为 undefined（调用方决定是否渲染）。
 */
export function resolveRunFailedText(payload: Record<string, unknown>): string | undefined {
  const error = payload.error as string | undefined;
  const endReason = payload.end_reason as string | undefined;
  if (endReason === "client_disconnected") {
    return "连接已中断，本次对话可能未完整保存。请检查网络或重新连接后重试。";
  }
  if (endReason === "max_steps_reached") {
    return "已达到最大步骤数，未产出最终回答。请精简任务范围或重试。";
  }
  return error;
}

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
  // 差异化失败文案判定收敛到 resolveRunFailedText（可单测的纯函数）。
  const errorText = resolveRunFailedText(payload);

  const durationMs = typeof payload.duration_ms === "number" ? payload.duration_ms : undefined;
  const inputTokens = typeof payload.input_tokens === "number" ? payload.input_tokens : undefined;
  const outputTokens = typeof payload.output_tokens === "number" ? payload.output_tokens : undefined;
  const totalTokens = typeof payload.total_tokens === "number" ? payload.total_tokens : undefined;
  const langfuseTraceId = typeof payload.langfuse_trace_id === "string" ? payload.langfuse_trace_id : undefined;

  return (
    <div className="flex justify-center py-2">
      <div className="flex flex-col items-center gap-2 rounded-md border border-border bg-card px-6 py-3">
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
  const { copied, copy } = useCopyToClipboard();

  const handleCopy = () => {
    // copy 内部已统一经 logWarn 记录失败，此处无需重复 catch。
    void copy(traceId);
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
