"use client";

import { Fragment, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ChevronRightIcon } from "lucide-react";

import { cn } from "@/lib/utils";

export type Crumb = { label: string; to?: string };

/**
 * 主区域唯一的页面头部：左侧面包屑（当前位置），中部状态，右侧操作。
 *
 * 所有页面（对话、系统配置、工作区配置、上下文观测）都通过 AppLayout 渲染它，
 * 页面自身不再各写标题栏或「返回」按钮——返回由面包屑承担。
 * 最后一项是当前页，渲染为 aria-current="page" 的标题，过长时截断并保留完整 title。
 *
 * 头部位于窗口最上方一行，整行是标题栏拖动区（可点击元素由 Tauri 拖拽脚本自动排除）。
 * 左右内边距读 `--header-inset-left/right`：由 AppLayout 按「此刻谁在窗口左上 / 右上角」
 * 换算自标题栏安全区，头部自身不判断平台与布局。
 */
export function PageHeader({
  crumbs,
  status,
  actions,
  leading,
  className,
}: {
  crumbs: Crumb[];
  status?: ReactNode;
  actions?: ReactNode;
  /** 标题前的控件（侧栏为图标栏时的展开开关）。 */
  leading?: ReactNode;
  className?: string;
}) {
  return (
    <header data-tauri-drag-region="deep" className={cn("flex h-14 shrink-0 items-center gap-3 pr-[max(1rem,var(--header-inset-right,0px))] pl-[max(1rem,var(--header-inset-left,0px))]", className)}>
      {leading}
      <nav aria-label="当前位置" className="min-w-0 flex-1">
        <ol className="flex min-w-0 items-center gap-1.5 text-sm">
          {crumbs.map((crumb, index) => {
            const last = index === crumbs.length - 1;
            return (
              <Fragment key={`${crumb.label}-${index}`}>
                {index > 0 && <ChevronRightIcon aria-hidden="true" className="text-muted-foreground size-3.5 shrink-0" />}
                <li className={cn("min-w-0", last ? "flex-1" : "shrink-0")}>
                  {last ? (
                    <span aria-current="page" title={crumb.label} className="block truncate font-medium">{crumb.label}</span>
                  ) : crumb.to ? (
                    <Link to={crumb.to} className="text-muted-foreground hover:text-foreground focus-ring block max-w-48 truncate rounded-sm">{crumb.label}</Link>
                  ) : (
                    <span className="text-muted-foreground block max-w-48 truncate">{crumb.label}</span>
                  )}
                </li>
              </Fragment>
            );
          })}
        </ol>
      </nav>
      {status && <div className="shrink-0">{status}</div>}
      {actions && <div className="flex shrink-0 items-center gap-1">{actions}</div>}
    </header>
  );
}
