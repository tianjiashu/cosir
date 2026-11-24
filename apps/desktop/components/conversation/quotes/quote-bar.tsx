import { ReplyIcon, XIcon } from "lucide-react";
import { useRef, useState, type KeyboardEvent } from "react";

import { StaticMarkdown } from "@/components/markdown/static-markdown";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { quoteHeadline, type QuoteCapsule } from "@/lib/assistant/capsules";
import { quoteSourceText } from "@/lib/workspace-references/ref-capsule";
import { isImeComposing } from "@/lib/keyboard/ime";

/**
 * 输入框上方的一条摘录：一行显示来源（来自文件时）与原文首行，有批注时带「带批注」角标，✕ 删除；点开是对话框，上面是完整原文，
 * 下面改批注（改动即时写回胶囊的 `note`，见 `quote-cards`）。原文不可编辑——改了就不是原话。
 *
 * 批注里回车只是「完成」（关掉对话框），Shift+回车换行；发送仍在输入框里。
 */
export function QuoteBar({ capsule, disabled, onNoteChange, onRemove }: {
  capsule: QuoteCapsule;
  disabled: boolean;
  onNoteChange: (note: string) => void;
  onRemove: () => void;
}) {
  const [open, setOpen] = useState(false);
  const composing = useRef(false);
  const headline = quoteHeadline(capsule.text);
  const source = quoteSourceText(capsule);
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey || isImeComposing(event, composing.current)) return;
    event.preventDefault();
    setOpen(false);
  };
  return (
    <div data-quote-bar="true" className="bg-muted/60 text-muted-foreground flex h-9 min-w-0 items-center gap-2 rounded-lg pr-1 pl-3 text-sm">
      <button
        type="button"
        aria-label={`查看摘录 ${headline}`}
        className="hover:text-foreground flex min-w-0 flex-1 items-center gap-2 text-left"
        onClick={() => setOpen(true)}
      >
        <ReplyIcon className="size-4 shrink-0" aria-hidden="true" />
        {source && <span className="text-foreground shrink-0">{source}</span>}
        <span className="min-w-0 truncate">“{headline}”</span>
        {capsule.note && <span className="bg-background/80 shrink-0 rounded-full px-2 py-0.5 text-xs">带批注</span>}
      </button>
      <button
        type="button"
        aria-label={`移除摘录 ${headline}`}
        disabled={disabled}
        className="hover:bg-background/80 hover:text-foreground grid size-7 shrink-0 place-items-center rounded-md disabled:opacity-50"
        onClick={onRemove}
      >
        <XIcon className="size-4" aria-hidden="true" />
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="gap-3 sm:max-w-lg">
          <DialogTitle>{source ? `摘录 · ${source}` : "摘录"}</DialogTitle>
          <div className="text-muted-foreground max-h-80 overflow-y-auto border-l-2 pl-3">
            <StaticMarkdown>{capsule.text}</StaticMarkdown>
          </div>
          <textarea
            aria-label="摘录批注"
            rows={2}
            autoFocus
            value={capsule.note ?? ""}
            placeholder="批注（可选，回车完成）"
            disabled={disabled}
            className="placeholder:text-muted-foreground field-sizing-content max-h-40 min-h-12 w-full resize-none rounded-md border px-2 py-1.5 leading-6 outline-none"
            onChange={(event) => onNoteChange(event.target.value)}
            onKeyDown={onKeyDown}
            onCompositionStart={() => { composing.current = true; }}
            onCompositionEnd={() => { composing.current = false; }}
          />
        </DialogContent>
      </Dialog>
    </div>
  );
}
