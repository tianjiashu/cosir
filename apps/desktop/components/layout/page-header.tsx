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
  /** 标题前的控件（窄屏下的侧栏开关）。 */
  leading?: ReactNode;
  className?: string;
}) {
  return (
    <header className={cn("flex h-14 shrink-0 items-center gap-3 border-b px-4", className)}>
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
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </header>
  );
}
