import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * 等用户卡：对话流里唯一带边框、唯一用信息蓝的容器（设计 §5.8：需要人动作、可能停留很久，深浅主题都要
 * 第一眼看到）。两处使用：等你操作的调用就地画成的卡（内容由 kind 视图的 `awaiting` 给出，见 `call-view.ts`，
 * 决定之后原地变回一行调用），与对话底部的终端批准卡（批准是进程内状态、可能来自子 Agent，没有调用位置可依附）。
 *
 * 版式固定为三段：`AwaitingHeader`（信息蓝色带：小字来由 + 标题）、正文、`AwaitingFooter`（操作靠右）。
 * 外框只管外观，不带调用 id：调用行的外层已经标了 `data-tool-call-id`，批准卡的调用 id 只在来源会话内唯一。
 */
export function AwaitingFrame({ title, className, children }: { title: string; className?: string; children: ReactNode }) {
  return (
    <section
      data-slot="awaiting-card"
      aria-label={`等你操作：${title}`}
      className={cn(
        "bg-background min-w-0 max-w-[40rem] overflow-hidden rounded-lg text-2sm",
        "border border-[color-mix(in_oklab,var(--info)_38%,transparent)]",
        className,
      )}
    >
      {children}
    </section>
  );
}

/**
 * 卡片页头：色带与边框同一个信息蓝，标题是卡片要用户决定的那件事；正文区用普通底色，读起来不像一整块告警。
 * `eyebrow` 是图标后的小字来由（提问的短标签、批准的来源），`aside` 放在右上角（提问翻页）。
 */
export function AwaitingHeader({ icon, eyebrow, title, aside }: { icon: ReactNode; eyebrow: ReactNode; title: ReactNode; aside?: ReactNode }) {
  return (
    <div className="flex min-w-0 items-start gap-3 bg-[color-mix(in_oklab,var(--info)_7%,transparent)] px-4 py-3">
      <div className="min-w-0 flex-1">
        <div className="text-info flex min-w-0 items-center gap-1.5 text-xs">
          {icon}
          {eyebrow}
        </div>
        <div className="text-chat mt-1 leading-6 font-semibold break-words">{title}</div>
      </div>
      {aside}
    </div>
  );
}

/** 卡片底栏：左侧放次要信息或入口，`children` 里用 `flex-1` 占位把操作推到右侧。 */
export function AwaitingFooter({ children }: { children: ReactNode }) {
  return <div className="border-border/60 flex min-w-0 items-center gap-1.5 border-t px-4 py-2.5">{children}</div>;
}
