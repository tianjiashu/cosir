/**
 * 后端错误横幅。
 *
 * 渲染两类提示，互不重叠：
 * 1. 后端启动失败的结构化错误卡（status 为 `failed` 且存在 `snapshot.lastError`）：
 *    展示失败摘要、详细原因、可展开的 traceback，以及「重启后端 / 查看日志」操作入口。
 * 2. 传输通道断开的非阻塞告警条（后端处于 `running` 或 `ready` 但 `transportError` 非空）：
 *    提示 SSE/IPC 通道中断且正在尝试恢复，区别于上面的启动失败错误卡。
 *
 * 不直连 IPC，状态来自 store，操作来自 `useBackend` Hook。
 *
 * @module components/backend/BackendErrorBanner
 */

import { useState } from "react";
import {
  AlertTriangle,
  ChevronDown,
  RotateCw,
  ScrollText,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Caption } from "@/components/ui/tokens";
import {
  useBackendStore,
  selectBackendStatus,
  selectBackendSnapshot,
} from "@/stores/backendStore";
import { useBackend } from "@/hooks/useBackend";
import { cn } from "@/lib/utils";
import { logError } from "@/lib/logger";

/** 后端错误横幅属性。 */
interface BackendErrorBannerProps {
  /** 点击「查看日志」时切换到日志诊断页的回调。 */
  onViewLogs: () => void;
}

/**
 * 后端错误横幅组件。
 *
 * 渲染优先级与互斥规则：
 * - 当 `status` 为 `failed` 且存在 `snapshot.lastError` 时，渲染结构化错误卡（致命、阻塞）。
 * - 当 `status` 为 `running` 或 `ready` 且 `transportError` 非空时，渲染非阻塞告警条，
 *   提示 SSE/IPC 通道中断；此时后端仍在运行，与启动失败错误卡互不叠加。
 * - 其余情况返回 `null`。
 *
 * @param props.onViewLogs - 切换到日志诊断页的回调。
 * @returns 结构化错误卡、`transportError` 告警条或 `null`。
 */
export function BackendErrorBanner({ onViewLogs }: BackendErrorBannerProps) {
  const status = useBackendStore(selectBackendStatus);
  const snapshot = useBackendStore(selectBackendSnapshot);
  const transportError = useBackendStore((s) => s.transportError);
  const { restart, isBusy } = useBackend();
  const [expanded, setExpanded] = useState(false);

  if (status === "running" || status === "ready") {
    if (transportError) {
      return (
        <div className="border-b border-amber-500/40 bg-amber-500/10 px-4 py-2">
          <div className="flex items-center gap-2 text-sm text-amber-600 dark:text-amber-400">
            <AlertTriangle className="h-4 w-4 shrink-0" />
            <span>后端连接中断：{transportError}（正在尝试恢复…）</span>
          </div>
        </div>
      );
    }
  }

  if (status !== "failed" || !snapshot?.lastError) {
    return null;
  }

  const error = snapshot.lastError;

  const handleRestart = () => {
    restart().catch((err) => {
      logError("从错误横幅重启后端失败", err, {
        module: "BackendErrorBanner",
      });
    });
  };

  return (
    <div className="border-b border-destructive/40 bg-destructive/10 px-4 py-3">
      <div className="flex items-start gap-3">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-destructive">{error.message}</p>
          {error.detail ? (
            <p className="mt-1 break-words text-xs text-muted-foreground">
              {error.detail}
            </p>
          ) : null}

          <div className="mt-2 flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant="outline"
              className="h-7 gap-1 text-xs"
              onClick={handleRestart}
              disabled={isBusy}
            >
              <RotateCw
                className={isBusy ? "h-3 w-3 animate-spin" : "h-3 w-3"}
              />
              重启后端
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="h-7 gap-1 text-xs"
              onClick={onViewLogs}
            >
              <ScrollText className="h-3 w-3" />
              查看日志
            </Button>
            {error.traceback ? (
              <Button
                size="sm"
                variant="ghost"
                className="h-7 gap-1 text-xs"
                onClick={() => setExpanded((value) => !value)}
                aria-expanded={expanded}
              >
                <ChevronDown
                  className={
                    expanded ? "h-3 w-3 rotate-180" : "h-3 w-3"
                  }
                />
                {expanded ? "收起堆栈" : "查看堆栈"}
              </Button>
            ) : null}
          </div>

          {expanded && error.traceback ? (
            <pre className={cn("mt-2 max-h-48 overflow-auto rounded bg-background/60 p-2 leading-relaxed text-muted-foreground", Caption.xs)}>
              {error.traceback}
            </pre>
          ) : null}

          <p className={cn("mt-1 text-muted-foreground/70", Caption.xs)}>
            {error.stage} · {error.occurredAt}
          </p>
        </div>
      </div>
    </div>
  );
}
