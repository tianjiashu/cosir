"use client";

import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { RenderErrorCard } from "./render-error-card";
import { RenderIsolationBoundary } from "./render-isolation-boundary";

type IsolatedRegionProps = {
  /** 稳定的区域标识，写入诊断日志 metadata。 */
  region: string;
  /** 展示给用户的区域名称。 */
  label: string;
  /** 变化时重置边界（例如切换 task / workspace）。 */
  resetKey: string;
  metadata?: Record<string, unknown>;
  fallbackClassName?: string;
  /** stack：卡片与离开按钮纵向排列（整页覆盖层）；row：横向排列（面板）。 */
  layout?: "stack" | "row";
  /**
   * 崩溃后的离开入口。没有它时，用户只能反复重试同一个崩溃。回调拿到 reset，
   * 由调用方决定是否在离开后重置边界（例如先关闭面板再重置，重新挂载时不再渲染崩溃内容）。
   */
  escape?: { label: string; onClick: (reset: () => void) => void };
  children: ReactNode;
};

/**
 * 页面级区域的渲染隔离：配置页、上下文观测、工作台、终端面板任一渲染崩溃只降级该区域，
 * 侧栏与正在运行的 Assistant 会话照常可用。消息与工具片段级别的隔离见 PartRenderBoundary。
 */
export function IsolatedRegion({
  region,
  label,
  resetKey,
  metadata,
  fallbackClassName,
  layout = "stack",
  escape,
  children,
}: IsolatedRegionProps) {
  return (
    <RenderIsolationBoundary
      scope="region"
      resetKey={resetKey}
      metadata={{ region, ...metadata }}
      fallback={({ onRetry }) => (
        <div className={fallbackClassName}>
          <div className={cn(layout === "row" ? "flex items-start gap-2" : "w-full max-w-md space-y-2")}>
            <div className="min-w-0 flex-1">
              <RenderErrorCard scope="region" label={label} onRetry={onRetry} />
            </div>
            {escape && (
              <Button type="button" variant="outline" size="sm" onClick={() => escape.onClick(onRetry)}>
                {escape.label}
              </Button>
            )}
          </div>
        </div>
      )}
    >
      {children}
    </RenderIsolationBoundary>
  );
}
