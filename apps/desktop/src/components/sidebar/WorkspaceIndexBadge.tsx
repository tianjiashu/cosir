/**
 * Workspace 索引状态徽标。
 *
 * 展示单个 workspace 的 CodeGraph 索引进度状态：空闲 / 索引中 / 已就绪 / 降级 / 错误。
 * 纯展示组件：状态数据来自 ``useWorkspaceIndexStore``，触发索引进度由
 * ``Sidebar`` 的 ``useEffect`` 负责（本组件不发起请求，保持单一职责）。
 *
 * @module components/sidebar/WorkspaceIndexBadge
 */

import { AlertTriangle, CheckCircle2, Loader2 } from "lucide-react";
import type { WorkspaceIndexStatus } from "@shared/codegraph";
import { Caption } from "@/components/ui/tokens";
import { cn } from "@/lib/utils";

/** 徽标展示配置（文案 + 样式），按状态归一化，避免在 JSX 里堆分支。 */
const BADGE_CONFIG: Record<
  WorkspaceIndexStatus["state"],
  { label: string; className: string; icon: "check" | "loading" | "warn" | "idle" | "error" }
> = {
  idle: { label: "未索引", className: "text-muted-foreground", icon: "idle" },
  preparing: { label: "索引中", className: "text-amber-500", icon: "loading" },
  ready: { label: "已就绪", className: "text-emerald-500", icon: "check" },
  degraded: { label: "已降级", className: "text-muted-foreground", icon: "warn" },
  error: { label: "索引失败", className: "text-destructive", icon: "error" },
};

/** WorkspaceIndexBadge 组件属性。 */
export interface WorkspaceIndexBadgeProps {
  /** workspace 的索引状态快照。 */
  status?: WorkspaceIndexStatus;
  /** 状态徽标渲染的可选标题（悬停提示）。 */
  title?: string;
}

/**
 * 渲染 workspace 索引进度状态徽标。
 *
 * @param props - 状态快照与可选悬停标题。
 * @returns 状态徽标元素；``status`` 为空时渲染为空（不占位）。
 */
export function WorkspaceIndexBadge({ status, title }: WorkspaceIndexBadgeProps) {
  if (!status) {
    return null;
  }
  const config = BADGE_CONFIG[status.state] ?? BADGE_CONFIG.idle;
  return (
    <span
      title={title ?? statusLabel(status)}
      className={cn(
        cn("inline-flex shrink-0 items-center gap-1 font-medium", Caption.xs10),
        config.className,
      )}
    >
      {config.icon === "loading" ? (
        <Loader2 className="h-3 w-3 animate-spin" />
      ) : config.icon === "check" ? (
        <CheckCircle2 className="h-3 w-3" />
      ) : config.icon === "warn" || config.icon === "error" ? (
        <AlertTriangle className="h-3 w-3" />
      ) : (
        <span className="h-1.5 w-1.5 rounded-full bg-current opacity-50" />
      )}
      {config.label}
    </span>
  );
}

/** 构造悬停提示文本（含降级原因等补充信息）。 */
function statusLabel(status: WorkspaceIndexStatus): string {
  switch (status.state) {
    case "ready":
      return `索引已就绪 · ${status.actionTaken ?? "init"}${
        status.filesChanged != null ? ` · ${status.filesChanged} 文件` : ""
      }`;
    case "degraded":
      return `CodeGraph 已降级：${status.degradedReason ?? "未知原因"}`;
    case "error":
      return "索引进度流连接失败，可尝试重建";
    default:
      return "";
  }
}
