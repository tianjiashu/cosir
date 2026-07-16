/**
 * 后端启动失败错误横幅。
 *
 * 当本地后端启动失败时，渲染结构化错误卡：展示失败摘要、详细原因、
 * 可展开的 traceback，以及「重启后端 / 查看日志」操作入口。
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
import {
  useBackendStore,
  selectBackendStatus,
  selectBackendSnapshot,
} from "@/stores/backendStore";
import { useBackend } from "@/hooks/useBackend";
import { logError } from "@/lib/logger";

/** 后端错误横幅属性。 */
interface BackendErrorBannerProps {
  /** 点击「查看日志」时切换到日志诊断页的回调。 */
  onViewLogs: () => void;
}

/**
 * 后端启动失败错误横幅组件。
 *
 * 仅在后端状态为 `failed` 且存在结构化错误时渲染；否则返回 `null`。
 *
 * @param props.onViewLogs - 切换到日志诊断页的回调。
 * @returns 结构化错误卡或 `null`。
 */
export function BackendErrorBanner({ onViewLogs }: BackendErrorBannerProps) {
  const status = useBackendStore(selectBackendStatus);
  const snapshot = useBackendStore(selectBackendSnapshot);
  const { restart, isBusy } = useBackend();
  const [expanded, setExpanded] = useState(false);

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
            <pre className="mt-2 max-h-48 overflow-auto rounded bg-background/60 p-2 text-[11px] leading-relaxed text-muted-foreground">
              {error.traceback}
            </pre>
          ) : null}

          <p className="mt-1 text-[11px] text-muted-foreground/70">
            {error.stage} · {error.occurredAt}
          </p>
        </div>
      </div>
    </div>
  );
}
