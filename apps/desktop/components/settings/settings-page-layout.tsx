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
    <div className="mx-auto flex h-full w-full max-w-6xl flex-col px-5 lg:flex-row lg:gap-8 lg:px-8">
      <nav className="flex shrink-0 gap-1 overflow-x-auto pt-6 lg:block lg:w-48 lg:space-y-1" aria-label={navLabel}>
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
      {/* 只有内容区滚动，导航不动。内容列至少撑满内容区且高度确定：普通面板随内容变长、由内容区滚动，
          文档类面板（Markdown 编辑）用 flex-1 填满剩余高度，保存栏不会被挤出视口。 */}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-y-auto pb-6">
        <div aria-hidden="true" className="scroll-fade-top" />
        {/* 上内边距在渐隐层之后：初始时渐隐只盖住留白，不盖住内容。 */}
        <div className="flex shrink-0 grow flex-col gap-4 pt-6">
          {intro}
          {children}
        </div>
      </div>
    </div>
  );
}
