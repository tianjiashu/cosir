import { ChevronDownIcon } from "lucide-react";
import { useRef, useState } from "react";

import { StaticMarkdown } from "@/components/markdown/static-markdown";
import { useClipped } from "@/hooks/use-clipped";
import type { QuoteCapsule } from "@/lib/assistant/capsules";
import { quoteSourceText } from "@/lib/workspace-references/ref-capsule";
import { cn } from "@/lib/utils";

/** 已发送消息里的一段摘录：来源（来自文件时）、左竖线引文（默认 3 行，可展开），批注紧跟其下；原文与批注读起来是一组。 */
export function QuoteBlock({ capsule }: { capsule: QuoteCapsule }) {
  const body = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const clipped = useClipped(body, !open, [capsule.text]);
  const source = quoteSourceText(capsule);
  return (
    <div data-quote-block="true" className="mb-2 flex min-w-0 flex-col gap-1">
      {source && <div className="text-muted-foreground text-xs">{source}</div>}
      <div
        ref={body}
        className={cn("text-muted-foreground min-w-0 border-l-2 pl-3", !open && "max-h-[calc(var(--prose-line-height)*3)] overflow-hidden")}
      >
        <StaticMarkdown>{capsule.text}</StaticMarkdown>
      </div>
      {(clipped || open) && (
        <button type="button" className="text-muted-foreground hover:text-foreground flex w-fit items-center gap-0.5 pl-3 text-xs" onClick={() => setOpen((value) => !value)}>
          {open ? "收起" : "展开"}
          <ChevronDownIcon className={cn("size-3 transition-transform", open && "rotate-180")} />
        </button>
      )}
      {capsule.note && <div className="whitespace-pre-wrap">{capsule.note}</div>}
    </div>
  );
}
