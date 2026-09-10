"use client";

import { GaugeIcon, InfoIcon } from "lucide-react";
import type { ReactElement } from "react";
import { useAuiState } from "@assistant-ui/react";

import {
  Popover,
  PopoverContent,
  PopoverDescription,
  PopoverHeader,
  PopoverTitle,
  PopoverTrigger,
} from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import type { TransportState, TransportRun } from "@/lib/assistant/contract";

type UsageTone = "normal" | "warning" | "critical" | "unknown";

export type ContextUsagePresentation = {
  tone: UsageTone;
  status: "normal" | "warning" | "critical" | "overage" | "unknown";
  percent: number | null;
  label: string;
  detail: string;
  remainingTokens: number | null;
  overageTokens: number | null;
  measured: boolean;
};

export function formatTokenCount(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "—";
  if (value < 1_000) return Math.round(value).toLocaleString("en-US");
  if (value < 1_000_000) {
    const compact = value / 1_000;
    const rounded = Number(compact.toFixed(compact < 10 ? 1 : 0));
    return rounded >= 1_000 ? "1M" : `${rounded.toLocaleString("en-US")}k`;
  }
  const compact = value / 1_000_000;
  const rounded = Number(compact.toFixed(compact < 10 ? 1 : 0));
  return `${rounded.toLocaleString("en-US")}M`;
}

export function getContextUsagePresentation(
  ratio: number,
  used: number | null,
  total: number | null,
): ContextUsagePresentation {
  const hasRatio = Number.isFinite(ratio) && ratio >= 0;
  const measured = used !== null && total !== null && total > 0;
  const hasAnyMeasurement = measured || ratio > 0;
  const percent = hasRatio ? Math.round(ratio * 100) : null;
  const status: ContextUsagePresentation["status"] = !hasRatio || !hasAnyMeasurement
    ? "unknown"
    : ratio > 1
      ? "overage"
      : ratio > 0.85
        ? "critical"
      : ratio >= 0.65
        ? "warning"
        : "normal";
  const tone: UsageTone = !hasRatio || !hasAnyMeasurement
    ? "unknown"
    : ratio > 0.85
      ? "critical"
      : ratio >= 0.65
        ? "warning"
        : "normal";
  if (!hasRatio || !hasAnyMeasurement) {
    return { tone, status, percent: null, label: "上下文 —", detail: "尚未完成有效测量", remainingTokens: null, overageTokens: null, measured: false };
  }
  if (measured) {
    const remainingTokens = Math.max(0, total - used);
    const overageTokens = Math.max(0, used - total);
    return {
      tone,
      status,
      percent,
      label: `上下文 ${percent}%`,
      detail: `${formatTokenCount(used)} / ${formatTokenCount(total)} tokens`,
      remainingTokens,
      overageTokens,
      measured: true,
    };
  }
  return {
    tone,
    status,
    percent,
    label: `上下文 ${percent}%`,
    detail: "已收到占用比例，绝对 token 尚不可用",
    remainingTokens: null,
    overageTokens: null,
    measured: false,
  };
}

export function shouldDisplayRunUsage(
  visible: boolean,
  messageRunId: number | null,
  runExists: boolean,
): boolean {
  return visible
    && messageRunId !== null
    && runExists;
}

function stateNumber(state: unknown, key: string): number | null {
  if (typeof state !== "object" || state === null) return null;
  const value = (state as Record<string, unknown>)[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stateNullableNumber(state: unknown, key: string): number | null {
  if (typeof state !== "object" || state === null) return null;
  const value = (state as Record<string, unknown>)[key];
  return value === null ? null : stateNumber(state, key);
}

function toneClass(tone: UsageTone): string {
  if (tone === "critical") return "bg-destructive";
  if (tone === "warning") return "bg-amber-500";
  if (tone === "normal") return "bg-primary";
  return "bg-muted-foreground/40";
}

function statusLabel(status: ContextUsagePresentation["status"]): string {
  if (status === "normal") return "正常";
  if (status === "warning") return "接近上限";
  if (status === "critical") return "高占用";
  if (status === "overage") return "已超出窗口";
  return "未测量";
}

export function TaskContextUsage(): ReactElement {
  const ratio = useAuiState((state) => stateNullableNumber(state.thread.state, "context_usage_ratio"));
  const used = useAuiState((state) => stateNullableNumber(state.thread.state, "context_usage_used"));
  const total = useAuiState((state) => stateNullableNumber(state.thread.state, "context_window_total"));
  const presentation = getContextUsagePresentation(ratio ?? Number.NaN, used, total);
  const percent = presentation.percent === null ? 0 : Math.min(100, Math.max(0, presentation.percent));

  return (
    <Popover>
      <PopoverTrigger
        openOnHover
        render={
          <button
            type="button"
            data-testid="task-context-usage"
            aria-label={presentation.label}
            className="text-muted-foreground hover:text-foreground inline-flex h-8 items-center gap-1.5 rounded-md px-2 text-xs transition-colors"
          />
        }
      >
        <GaugeIcon className="size-3.5" aria-hidden="true" />
        <span className="hidden sm:inline">{presentation.label}</span>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-64">
        <PopoverHeader>
          <PopoverTitle>上下文窗口</PopoverTitle>
          <PopoverDescription>{presentation.detail}</PopoverDescription>
        </PopoverHeader>
        <div className="flex items-center justify-between text-xs">
          <span className={cn(
            presentation.status === "overage" && "text-destructive",
            presentation.status === "critical" && "text-destructive",
            presentation.status === "warning" && "text-amber-600 dark:text-amber-400",
            presentation.status === "normal" && "text-primary",
            "font-medium",
          )}>{statusLabel(presentation.status)}</span>
          {presentation.measured && (
            <span className="text-muted-foreground">
              {presentation.overageTokens && presentation.overageTokens > 0
                ? `超出 ${formatTokenCount(presentation.overageTokens)}`
                : `剩余 ${formatTokenCount(presentation.remainingTokens ?? 0)}`}
            </span>
          )}
        </div>
        <div
          role="progressbar"
          aria-label="上下文窗口占用"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={presentation.status === "unknown" ? undefined : percent}
          aria-valuetext={
            presentation.status === "unknown"
              ? "尚未完成有效测量"
              : presentation.status === "overage"
                ? `${presentation.label}，已超出窗口`
                : presentation.label
          }
          className="bg-muted h-2 overflow-hidden rounded-full"
        >
          <div className={cn("h-full rounded-full transition-[width]", toneClass(presentation.tone))} style={{ width: `${percent}%` }} />
        </div>
        <div className="text-muted-foreground flex items-start gap-1.5 text-xs">
          <InfoIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
          <span>按当前有效上下文估算，不等同于计费 token。</span>
        </div>
      </PopoverContent>
    </Popover>
  );
}

export function RunUsageDisplay({ runId, visible }: { runId: number | null; visible: boolean }): ReactElement | null {
  const run = useAuiState((state): TransportRun | null => {
    const transportState = state.thread.state as unknown as TransportState;
    return runId === null ? null : transportState.runs.find((candidate) => candidate.runId === runId) ?? null;
  });
  const usage = run?.usage ?? null;
  const input = usage?.input_tokens ?? null;
  const output = usage?.output_tokens ?? null;
  const total = usage?.total_tokens ?? null;
  const cacheHit = usage?.cache_hit_tokens ?? null;
  const cacheMiss = usage?.cache_miss_tokens ?? null;
  const reasoning = usage?.reasoning_tokens ?? null;

  if (!shouldDisplayRunUsage(visible, runId, run !== null)) return null;
  const known = usage !== null;
  const isRunning = run?.status === "pending" || run?.status === "running";
  const label = known ? `本次用量 ${formatTokenCount(total ?? 0)} tokens` : isRunning ? "用量统计中…" : "本次用量 —";

  return (
    <Popover>
      <PopoverTrigger
        openOnHover
        render={<button type="button" data-testid="run-usage-display" className="text-muted-foreground hover:text-foreground mt-1 rounded px-1 text-xs transition-colors" aria-label={label} />}
      >
        {label}
      </PopoverTrigger>
      <PopoverContent align="start" className="w-64">
        <PopoverHeader>
          <PopoverTitle>本次 Run 用量</PopoverTitle>
          <PopoverDescription>{known ? "模型返回的 provider usage 累计值；重启或断连时可能不完整" : "当前 Run 尚未提供可用用量"}</PopoverDescription>
        </PopoverHeader>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
          <dt className="text-muted-foreground">输入</dt><dd className="text-right">{known ? formatTokenCount(input ?? 0) : "—"}</dd>
          <dt className="text-muted-foreground">输出</dt><dd className="text-right">{known ? formatTokenCount(output ?? 0) : "—"}</dd>
          <dt className="text-muted-foreground">总计</dt><dd className="text-right">{known ? formatTokenCount(total ?? 0) : "—"}</dd>
          <dt className="text-muted-foreground">缓存命中</dt><dd className="text-right">{known ? formatTokenCount(cacheHit ?? 0) : "—"}</dd>
          <dt className="text-muted-foreground">缓存未命中</dt><dd className="text-right">{known && cacheMiss !== null ? formatTokenCount(cacheMiss) : "—"}</dd>
          <dt className="text-muted-foreground">推理</dt><dd className="text-right">{known ? formatTokenCount(reasoning ?? 0) : "—"}</dd>
        </dl>
      </PopoverContent>
    </Popover>
  );
}
