/**
 * 上下文窗口占用圆环（输入侧 token 估算）。
 *
 * 渲染后端 CONTEXT_USAGE 事件下发的「当前上下文窗口已用 token / 实际上限」：
 * 以 SVG 圆环节省空间，百分比文字辅以 token 计数。颜色按占用率分级
 * （安全 / 警告 ≥80% / 危险 ≥95%），与用户直觉一致。圆环数据来源为
 * RuntimeContext.messages 本地估算，turn 取消也不丢，故圆环反映真实余量。
 *
 * @module components/chat/ContextUsageRing
 */

import { EMPTY_USAGE, useContextUsageStore } from "@/stores/contextUsageStore";
import { useTaskStore } from "@/stores/taskStore";

/** 将 token 数格式化为带一位小数的 K/M 单位（如 66.5K / 1.0M）。 */
function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return `${n}`;
}

/** 圆环颜色分级：安全 / 警告 / 危险。 */
function usageColor(pct: number): string {
  if (pct >= 0.95) return "text-red-500";
  if (pct >= 0.8) return "text-amber-500";
  return "text-emerald-500";
}

/** 上下文占用圆环组件（直径默认 16px，适配输入框底栏）。 */
export function ContextUsageRing({ size = 16, strokeWidth = 2 }: { size?: number; strokeWidth?: number }) {
  // 只展示当前活跃任务的占用：后台并发任务的 CONTEXT_USAGE 事件同样会写入 store
  // （按 taskId 分键），但不得出现在用户当前对话的圆环上。
  const activeTaskId = useTaskStore((s) => s.activeTaskId);
  // 缺省值必须用模块级共享常量：zustand 以引用相等判定变更，selector 内新建对象
  // 会让组件在每次 store 变更下都重渲染。
  const usage = useContextUsageStore((s) =>
    activeTaskId === null ? EMPTY_USAGE : s.usageByTaskId[activeTaskId] ?? EMPTY_USAGE,
  );
  const usedTokens = usage.usedTokens;
  const totalTokens = usage.totalTokens;

  const pct = totalTokens <= 0 ? 0 : Math.max(0, Math.min(1, usedTokens / totalTokens));
  const radius = (size - strokeWidth) / 2;
  const circumference = 2 * Math.PI * radius;
  const dashOffset = circumference * (1 - pct);
  const color = usageColor(pct);
  const percentLabel = (pct * 100).toFixed(1);

  return (
    <div
      className={`inline-flex items-center gap-1.5 text-xs ${color}`}
      role="status"
      aria-label={`上下文已使用 ${percentLabel}%`}
      title={`上下文已使用 ${usedTokens} / ${totalTokens} token`}
    >
      <svg
        width={size}
        height={size}
        viewBox={`0 0 ${size} ${size}`}
        className="-rotate-90"
        aria-hidden
      >
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="currentColor"
          strokeOpacity={0.15}
          strokeWidth={strokeWidth}
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="currentColor"
          strokeWidth={strokeWidth}
          strokeDasharray={circumference}
          strokeDashoffset={dashOffset}
          strokeLinecap="round"
          style={{ transition: "stroke-dashoffset 300ms ease-out" }}
        />
      </svg>
      <span>{percentLabel}%</span>
      <span className="text-muted-foreground">·</span>
      <span className="text-muted-foreground">
        {formatTokens(usedTokens)} / {formatTokens(totalTokens)} 上下文
      </span>
    </div>
  );
}
