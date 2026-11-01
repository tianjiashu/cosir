import { useRef, useState } from "react";
import { ChevronRightIcon } from "lucide-react";
import { MessagePrimitive, useScrollLock } from "@assistant-ui/react";

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { processKey, useDisclosure } from "@/lib/assistant/disclosure-store";
import { formatDuration } from "@/lib/assistant/run-duration";
import type { ProcessItem, ProcessLayout, TurnPhase } from "@/lib/assistant/turn-layout";
import { cn } from "@/lib/utils";
import { TEXT_PART_COMPONENTS } from "./markdown-part";
import { ReasoningItem } from "./reasoning-item";
import { DISCLOSURE_ANIMATION_MS, DISCLOSURE_CONTENT_CLASS, PROCESS_BODY_INDENT_CLASS } from "./turn-tokens";

export type ProcessBlockProps = {
  taskId: number;
  messageId: string;
  phase: TurnPhase;
  process: ProcessLayout;
  /** 回合耗时（毫秒）：结束后取 `outcome.durationMs`，运行中由父组件本地计时；没有时区头不显示耗时。 */
  durationMs: number | null;
  /** 调用行由父组件按 part 下标渲染（要读 assistant-ui 的 part 拿 args 与 artifact）。 */
  renderCall: (item: Extract<ProcessItem, { kind: "call" }>) => React.ReactNode;
};

/** 区头文案的纯函数部分：当前动作 / 已处理 + 耗时 + 按动词计数 + 失败、取消计数，供渲染与测试共用。 */
export function processHeading(phase: TurnPhase, process: ProcessLayout, durationMs: number | null): {
  lead: string;
  counts: string;
  problems: string | null;
} {
  const duration = durationMs === null ? null : formatDuration(durationMs);
  const lead = phase === "running"
    ? process.currentAction ?? "思考中"
    : phase === "suspended"
      ? "已暂停 · 等你操作"
      : duration === null ? "已处理" : `已处理 ${duration}`;
  const counts = process.verbCounts.map(({ label, count }) => `${label} ${count}`).join(" · ");
  const problems = [
    process.failedCount > 0 ? `${process.failedCount} 个失败` : null,
    process.cancelledCount > 0 ? `${process.cancelledCount} 个取消` : null,
  ].filter(Boolean).join(" · ");
  return { lead, counts, problems: problems || null };
}

/**
 * 过程区：区头（当前动作 / 已处理 + 计数）+ 区体（叙述、思考、调用按原序）。
 *
 * 只有 ≤1 次调用时不加区头、直接平铺（区头只会复述那一行，还要多点一次）。默认开合：运行中展开、否则收起，
 * 用户点过之后以 disclosure store 里的覆盖为准。运行中区体只露最近几步，更早的折进「前面还有 N 步」（本地
 * state，点开后显示全部）；区体不画竖线，只靠缩进与区头文字对齐。
 */
export function ProcessBlock({ taskId, messageId, phase, process, durationMs, renderCall }: ProcessBlockProps) {
  const running = phase === "running";
  const [open, setOpen] = useDisclosure(taskId, processKey(messageId), running);
  const [showEarlier, setShowEarlier] = useState(false);
  const collapsibleRef = useRef<HTMLDivElement>(null);
  const lockScroll = useScrollLock(collapsibleRef, DISCLOSURE_ANIMATION_MS);
  if (process.items.length === 0) return null;

  const renderItem = (item: ProcessItem) => {
    switch (item.kind) {
      case "narration":
        return (
          <div key={item.index} data-slot="narration" className="text-foreground my-1 text-sm">
            <MessagePrimitive.PartByIndex index={item.index} components={TEXT_PART_COMPONENTS} />
          </div>
        );
      case "reasoning":
        return <ReasoningItem key={item.index} taskId={taskId} messageId={messageId} index={item.index} part={item.part} />;
      case "call":
        return <div key={item.index}>{renderCall(item)}</div>;
    }
  };

  if (process.flat) {
    return <div data-slot="process-flat" className="flex flex-col gap-0.5">{process.items.map(renderItem)}</div>;
  }

  const heading = processHeading(phase, process, durationMs);
  const visible = running && !showEarlier ? process.tail : process.items;
  const earlierCount = running && !showEarlier ? process.earlierCount : 0;

  return (
    <Collapsible
      ref={collapsibleRef}
      open={open}
      onOpenChange={(next) => {
        lockScroll();
        setOpen(next);
      }}
      style={{ "--animation-duration": `${DISCLOSURE_ANIMATION_MS}ms` } as React.CSSProperties}
      data-slot="process-block"
      data-phase={phase}
    >
      <CollapsibleTrigger
        data-slot="process-head"
        className="group/process-head text-muted-foreground hover:text-foreground flex min-h-[30px] w-full min-w-0 items-center gap-2 text-left text-2sm"
      >
        <ChevronRightIcon
          className={cn("size-3.5 shrink-0 transition-transform duration-(--animation-duration) motion-reduce:transition-none", open && "rotate-90")}
          aria-hidden="true"
        />
        <span className="flex min-w-0 flex-1 flex-wrap items-center gap-x-1.5">
          <span
            data-slot="process-lead"
            className={cn("truncate", running && "text-foreground shimmer motion-reduce:animate-none")}
          >
            {heading.lead}
          </span>
          {heading.counts && <span data-slot="process-counts" className="truncate">· {heading.counts}</span>}
          {heading.problems && <span data-slot="process-problems" className="text-destructive shrink-0">· {heading.problems}</span>}
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent className={DISCLOSURE_CONTENT_CLASS}>
        <div data-slot="process-body" className={cn("mt-0.5 mb-1.5 flex min-w-0 flex-col gap-0.5", PROCESS_BODY_INDENT_CLASS)}>
          {earlierCount > 0 && (
            <button
              type="button"
              data-slot="process-earlier"
              className="text-muted-foreground hover:text-foreground -mx-1.5 flex min-h-[26px] items-center gap-1.5 rounded-md px-1.5 text-left text-xs"
              onClick={() => setShowEarlier(true)}
            >
              <ChevronRightIcon className="size-3.5 shrink-0" aria-hidden="true" />
              前面还有 {earlierCount} 步
            </button>
          )}
          {visible.map(renderItem)}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
