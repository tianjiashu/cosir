"use client";

import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";

import type { SettingsTab } from "@/components/settings/settings-tabs";
import { cn } from "@/lib/utils";

/**
 * 配置中心（系统级 / 工作区级）的统一页面布局：左侧标签导航 + 右侧面板。
 *
 * 标签是路由（`${basePath}/${id}`），可直接链接、前进后退；切换标签时的未保存修改
 * 由 AppLayout 的路由拦截统一询问，这里不再自行确认。页面标题与返回由 PageHeader 承担。
 */
export function SettingsPageLayout<Id extends string>({
  tabs,
  basePath,
  navLabel,
  intro,
  children,
}: {
  tabs: readonly SettingsTab<Id>[];
  basePath: string;
  navLabel: string;
  /** 面板上方的说明（例如当前配置的是哪个工作区）。 */
  intro?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto grid w-full max-w-6xl gap-6 px-5 py-6 lg:grid-cols-[12rem_minmax(0,1fr)] lg:gap-8 lg:px-8">
        <nav className="flex gap-1 overflow-x-auto lg:sticky lg:top-6 lg:block lg:space-y-1 lg:self-start" aria-label={navLabel}>
          {tabs.map(({ id, label, icon: Icon }) => (
            <NavLink
              key={id}
              to={`${basePath}/${id}`}
              className={({ isActive }) => cn(
                "focus-ring flex shrink-0 items-center gap-2.5 rounded-lg px-3 py-2 text-sm whitespace-nowrap transition-colors lg:w-full",
                isActive ? "bg-muted text-foreground font-medium" : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
              )}
            >
              <Icon className="size-4 shrink-0" aria-hidden="true" />
              <span className="truncate">{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="min-w-0 space-y-4">
          {intro}
          {children}
        </div>
      </div>
    </div>
  );
}
