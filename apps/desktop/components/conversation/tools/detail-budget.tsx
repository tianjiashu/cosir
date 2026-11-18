import { useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { ChevronDownIcon } from "lucide-react";

import { useClipped } from "@/hooks/use-clipped";
import { cn } from "@/lib/utils";

/** 工具详情收起时的最大高度（px）：约一屏的三分之一，长列表、长 diff、长文本都先给这么多。 */
export const DETAIL_BUDGET_HEIGHT = 384;

/**
 * 工具详情的高度上限：超出就截断、底部渐隐，给「显示全部 / 收起」；放得下时不出现入口。
 *
 * 只管高度。条数与体积的上限在后端（列表 `limit` 上限、patch 超限不下发），这里不截数据，避免「前端看不到、
 * 后端照样整份下发」。展开后整段铺开而不是在框内滚动：对话本身在滚，再嵌一个滚动区会吞掉滚轮。横向仍可
 * 滚动（diff 长行）。开合是这块详情的阅读状态，不进开合 store：详情首次展开后保留 DOM（`DeferredDetail`），
 * 收起整条调用再打开时沿用。
 *
 * `anchor="end"`：收起时留末尾、顶部渐隐——终端输出是「最后几行」，有用的是结尾（结果、报错、提示符）。
 * 做法是收起时把盒子滚到底，而不是用 flex 贴底：贴底时溢出在上方，不计入 `scrollHeight`，截断就测不出来。
 */
export function DetailBudget({ children, className, anchor = "start" }: { children: ReactNode; className?: string; anchor?: "start" | "end" }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  // 盒子不变而内容变长（运行中的终端输出）时观察者不触发，内容身份放进依赖。
  const clipped = useClipped(ref, !open, [children]);
  useLayoutEffect(() => {
    if (anchor === "end" && !open && ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [anchor, open, clipped, children]);
  const id = useId();
  return (
    <div data-slot="detail-budget">
      <div
        ref={ref}
        id={id}
        data-detail-collapsed={(clipped && !open) || undefined}
        className={cn(
          "overflow-x-auto", !open && "overflow-y-hidden",
          clipped && !open && (anchor === "end" ? "mask-t-from-[calc(100%-3rem)]" : "mask-b-from-[calc(100%-3rem)]"),
          className,
        )}
        style={open ? undefined : { maxHeight: DETAIL_BUDGET_HEIGHT }}
      >
        {children}
      </div>
      {clipped && (
        <button
          type="button"
          aria-expanded={open}
          aria-controls={id}
          onClick={() => setOpen(!open)}
          className="text-muted-foreground hover:text-foreground focus-visible:ring-ring/50 mt-1 inline-flex h-6 items-center gap-1 rounded-sm text-xs outline-none focus-visible:ring-2"
        >
          {open ? "收起" : "显示全部"}
          <ChevronDownIcon aria-hidden="true" className={cn("size-3.5 transition-transform", open && "rotate-180")} />
        </button>
      )}
    </div>
  );
}
