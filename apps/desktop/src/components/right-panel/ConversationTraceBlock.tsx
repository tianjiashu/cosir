/**
 * 当前对话 trace 诊断区块。
 *
 * @module components/right-panel/ConversationTraceBlock
 */

import { Activity, FileText } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useTaskStore } from "@/stores/taskStore";
import { useConversationTraceStore, type ConversationTraceRecord } from "@/stores/conversationTraceStore";

/** ConversationTraceBlock 组件属性。 */
interface ConversationTraceBlockProps {
  /** 打开日志页面。 */
  onOpenLogs: () => void;
}

/**
 * 当前对话 trace 诊断区块。
 *
 * @param props - 组件属性。
 * @returns 当前 active task 最近一次对话请求 trace 的只读展示。
 */
export function ConversationTraceBlock({ onOpenLogs }: ConversationTraceBlockProps) {
  const activeTaskId = useTaskStore((state) => state.activeTaskId);
  const streamTrace = useConversationTraceStore((state) =>
    activeTaskId ? state.streamTraceByTaskId[activeTaskId] ?? null : null,
  );
  const latestTrace = useConversationTraceStore((state) =>
    activeTaskId ? state.latestTraceByTaskId[activeTaskId] ?? null : null,
  );
  const displayTrace = streamTrace ?? latestTrace;

  return (
    <section className="space-y-2">
      <div className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
        <Activity className="h-3.5 w-3.5" />
        对话 Trace
      </div>

      {displayTrace ? (
        <div className="space-y-2 rounded-md border border-border bg-card p-2">
          <TraceField label={streamTrace ? "stream_trace_id" : "trace_id"} value={displayTrace.traceId} />
          <div className="grid grid-cols-2 gap-2 text-[11px] text-muted-foreground">
            <TraceMeta label="来源" value={operationLabel(displayTrace.operation)} />
            <TraceMeta label="方法" value={displayTrace.method} />
          </div>
          {streamTrace && latestTrace && latestTrace.traceId !== streamTrace.traceId ? (
            <TraceField label="latest_trace_id" value={latestTrace.traceId} />
          ) : null}
          <Button variant="outline" size="sm" className="h-7 w-full gap-1.5 text-xs" onClick={onOpenLogs}>
            <FileText className="h-3.5 w-3.5" />
            查看日志
          </Button>
        </div>
      ) : (
        <div className="rounded-md border border-dashed border-border p-3 text-xs text-muted-foreground">
          当前对话尚未产生 trace
        </div>
      )}
    </section>
  );
}

/** TraceField 组件属性。 */
interface TraceFieldProps {
  /** 字段标签。 */
  label: string;
  /** 字段值。 */
  value: string;
}

/**
 * 渲染单个 trace 长文本字段。
 *
 * @param props - 字段属性。
 * @returns 单个 trace 字段。
 */
function TraceField({ label, value }: TraceFieldProps) {
  return (
    <div className="space-y-1">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="break-all rounded bg-muted px-2 py-1 font-mono text-[11px] leading-4 text-foreground">
        {value}
      </div>
    </div>
  );
}

/**
 * 渲染一个短 trace 元信息字段。
 *
 * @param props - 字段属性。
 * @returns 单个短字段。
 */
function TraceMeta({ label, value }: TraceFieldProps) {
  return (
    <div className="min-w-0">
      <div>{label}</div>
      <div className="truncate font-mono text-foreground">{value}</div>
    </div>
  );
}

/**
 * 返回 trace 操作的人类可读标签。
 *
 * @param operation - trace 操作类型。
 * @returns 短标签。
 */
function operationLabel(operation: ConversationTraceRecord["operation"]): string {
  const labels: Record<ConversationTraceRecord["operation"], string> = {
    task_create: "创建",
    workspace_create: "工作区",
    workspace_tasks: "任务列表",
    workspace_delete: "删工作区",
    turn_create: "新轮次",
    task_turns: "轮次",
    turn_stream: "轮次 SSE",
    task_get: "查询",
    task_events: "事件",
    task_checkpoints: "检查点",
    task_cancel: "取消",
    task_stream: "SSE",
    task_approvals: "审批",
    approval_decision: "决策",
  };
  return labels[operation];
}
