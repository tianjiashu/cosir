/**
 * 顶部任务栏（TopBar）。
 *
 * 展示：
 * - 当前项目/任务标题
 * - 项目图标
 * - 运行状态展示（处理中/已完成/失败/已取消）
 * - 更多操作入口（占位）
 *
 * @module components/layout/TopBar
 */

import type { ComponentType } from "react";
import { useTaskStore, selectActiveTask, selectActiveTaskStatus } from "@/stores/taskStore";
import { useBackendStore, selectBackendSnapshot, selectBackendStatus } from "@/stores/backendStore";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  MoreHorizontal,
  ExternalLink,
  GitBranch,
  Loader2,
  CheckCircle2,
  XCircle,
  Ban,
} from "lucide-react";

/** 任务状态到视觉配置的映射。 */
const STATUS_CONFIG: Record<string, { label: string; variant: "default" | "secondary" | "destructive" | "warning" | "outline" | "success"; Icon: ComponentType<{ className?: string }> }> = {
  running: { label: "处理中", variant: "warning", Icon: Loader2 },
  completed: { label: "已完成", variant: "success", Icon: CheckCircle2 },
  failed: { label: "失败", variant: "destructive", Icon: XCircle },
  cancelled: { label: "已取消", variant: "outline", Icon: Ban },
  pending: { label: "等待中", variant: "secondary", Icon: GitBranch },
};

/**
 * TopBar 顶部任务栏组件。
 *
 * 显示当前任务标题、运行状态和操作按钮。
 * 状态标签颜色随任务状态动态变化。
 */
export function TopBar() {
  const activeTask = useTaskStore(selectActiveTask);
  const status = useTaskStore(selectActiveTaskStatus);
  const backendStatus = useBackendStore(selectBackendStatus);
  const backendSnapshot = useBackendStore(selectBackendSnapshot);

  const config = status ? STATUS_CONFIG[status] ?? STATUS_CONFIG.pending : null;
  const StatusIcon = config?.Icon ?? GitBranch;
  const backendBadge = {
    running: { label: "后端运行中", variant: "success" as const },
    starting: { label: "后端启动中", variant: "warning" as const },
    stopping: { label: "后端停止中", variant: "secondary" as const },
    restarting: { label: "后端重启中", variant: "warning" as const },
    failed: { label: "后端异常", variant: "destructive" as const },
    stopped: { label: "后端未启动", variant: "outline" as const },
  }[backendStatus];

  return (
    <header className="flex h-12 items-center gap-3 border-b border-border bg-background px-4">
      {/* 项目图标 + 标题 */}
      <div className="flex min-w-0 flex-1 items-center gap-2">
        <span className="text-lg">📋</span>
        <span className="truncate text-sm font-medium">
          {activeTask?.input_text ?? "调研项目定位"}
        </span>
      </div>

      {/* 运行状态标签 */}
      <div className="flex items-center gap-2 shrink-0">
        {config && (
          <Badge variant={config.variant} className="gap-1 shrink-0">
            <StatusIcon className={config.label === "处理中" ? "h-3 w-3 animate-spin" : "h-3 w-3"} />
            {config.label}
          </Badge>
        )}
        <Badge variant={backendBadge.variant} className="shrink-0">
          {backendBadge.label}
        </Badge>
        {backendSnapshot?.health?.modelName ? (
          <span className="hidden text-xs text-muted-foreground md:inline">
            {backendSnapshot.health.modelProvider} / {backendSnapshot.health.modelName}
          </span>
        ) : null}
      </div>

      {/* 右侧操作区（占位） */}
      <div className="flex items-center gap-1 shrink-0">
        {/* 环境信息：变更 / 本地 / main 等（参考截图） */}
        <div className="hidden items-center gap-3 text-xs text-muted-foreground sm:flex">
          <span className="flex items-center gap-1">
            📂 变更
          </span>
          <span className="flex items-center gap-1">
            💾 本地
          </span>
          <span className="flex items-center gap-1">
            <GitBranch className="h-3 w-3" />
            main
          </span>
          <span className="flex items-center gap-1">
            🔌 提交或推送
          </span>
        </div>

        {/* 打开位置按钮 */}
        <Button variant="ghost" size="sm" className="gap-1 h-8 text-xs">
          <ExternalLink className="h-3.5 w-3.5" />
          打开位置
        </Button>

        {/* 更多操作（占位） */}
        <Button variant="ghost" size="icon" className="h-8 w-8">
          <MoreHorizontal className="h-4 w-4" />
        </Button>
      </div>
    </header>
  );
}
