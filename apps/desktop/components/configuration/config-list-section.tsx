import type { ReactNode } from "react";
import { InfoIcon } from "lucide-react";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

/**
 * 配置页清单的一节（Skills、MCP 服务共用）：标题行是称呼 · 数量 · ⓘ 说明 · 只读标记 · 右侧位置，下面是调用方的内容。
 *
 * 这类页面用来浏览和开关，视觉焦点要落在条目名称上：不用卡片外框和分隔线，次要说明放进 ⓘ 提示，
 * 过长的位置截断、悬停看全。
 */
export function ConfigListSection({ title, count, info, readOnly = false, location, children }: {
  title: string;
  count: number;
  /** ⓘ 提示里的说明（谁能看到、配置写在哪等）。 */
  info: ReactNode;
  /** 继承来的一节：在这一页只能看，不能改。 */
  readOnly?: boolean;
  location?: string;
  children: ReactNode;
}) {
  return (
    <section className="space-y-1">
      <div className="flex min-w-0 items-center gap-1.5 px-2 text-xs">
        <h3 className="text-foreground shrink-0 text-sm font-medium">{title}</h3>
        <span className="text-muted-foreground shrink-0">{count}</span>
        <Tooltip>
          <TooltipTrigger aria-label={`${title}说明`} className="text-muted-foreground hover:text-foreground focus-ring shrink-0 rounded-sm">
            <InfoIcon className="size-3.5" />
          </TooltipTrigger>
          <TooltipContent>{info}</TooltipContent>
        </Tooltip>
        {readOnly && <span className="text-muted-foreground shrink-0">· 只读</span>}
        {location && <code className="text-muted-foreground ml-auto min-w-0 truncate" title={location}>{location}</code>}
      </div>
      {children}
    </section>
  );
}
