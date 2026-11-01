import { useState } from "react";
import { ListTreeIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTitle, PopoverTrigger } from "@/components/ui/popover";
import { markdownOutline, type OutlineHeading } from "@/lib/markdown-outline";
import { cn } from "@/lib/utils";

/**
 * Markdown 的大纲按钮：弹出层按层级列出标题，选中后交给 `onSelect` 定位（视图相关，由阅读器决定）。
 * 打开时才解析当前文本，平时不为大纲付出解析成本。分段读取的大文件只能看到当前分段的标题。
 */
export function OutlineButton({ text, partial, onSelect }: {
  text: string;
  partial: boolean;
  onSelect: (heading: OutlineHeading) => void;
}) {
  const [headings, setHeadings] = useState<OutlineHeading[] | null>(null);
  const [open, setOpen] = useState(false);
  const minDepth = headings?.length ? Math.min(...headings.map((heading) => heading.depth)) : 1;
  return (
    <Popover open={open} onOpenChange={(next) => {
      if (next) setHeadings(markdownOutline(text));
      setOpen(next);
    }}>
      <PopoverTrigger render={<Button size="icon-sm" variant="ghost" className="size-8" />} aria-label="大纲" title="大纲">
        <ListTreeIcon />
      </PopoverTrigger>
      <PopoverContent align="end" className="max-h-96 gap-1 overflow-auto p-1.5">
        <PopoverTitle className="px-1.5 pt-1 text-xs text-muted-foreground">{partial ? "大纲 · 仅当前分段" : "大纲"}</PopoverTitle>
        {headings?.length ? (
          <nav aria-label="文档大纲" className="flex flex-col">
            {headings.map((heading) => (
              <button
                key={heading.line}
                type="button"
                className={cn(
                  "truncate rounded py-1 pr-2 text-left text-sm hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring",
                  heading.depth === minDepth && "font-medium",
                )}
                style={{ paddingLeft: `${0.5 + (heading.depth - minDepth) * 0.875}rem` }}
                title={heading.text}
                onClick={() => {
                  setOpen(false);
                  onSelect(heading);
                }}
              >
                {heading.text}
              </button>
            ))}
          </nav>
        ) : (
          <p className="px-1.5 py-2 text-xs text-muted-foreground">没有标题</p>
        )}
      </PopoverContent>
    </Popover>
  );
}
