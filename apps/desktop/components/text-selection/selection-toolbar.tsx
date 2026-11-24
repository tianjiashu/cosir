import { TextQuoteIcon } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent } from "@/components/ui/popover";
import { useReferenceBridge } from "@/components/workspace-references/reference-bridge";
import { quoteHeadline } from "@/lib/assistant/capsules";
import { isImeComposing } from "@/lib/keyboard/ime";
import { shortcutLabel, useAppHotkey } from "@/lib/keyboard/shortcuts";
import { frontendLog } from "@/lib/logging/frontend-log";

import type { TextSelectionState } from "./use-text-selection";

/** 评论框打开期间，原选区的替身高亮（焦点进了评论框，页面上的原选区会消失）。 */
const PENDING_HIGHLIGHT = "cosir-selection-pending";

/**
 * 「选中 → 对话」的浮条：选区确定后在选区末端出现「摘录」；点它或按 Cmd+L 展开评论框，回车把摘录连同批注
 * （`note`）经 `reference-bridge` 加进当前会话输入框上方的摘录条，Esc 放弃。对话回复与文件阅读器共用，各界面只
 * 提供选区来源（`useDomSelectionSource` 或编辑器选区）。
 *
 * 显隐只看 `state.pending`：按钮状态下弹层的「点到外面」不关它（拖选结束的那次 click 也算点到外面，曾让按钮
 * 一出现就被关掉），选区折叠才消失。评论框里写了字时点到别处不关，空着就关。
 */
export function SelectionToolbar({ state, workspaceId, source }: {
  state: TextSelectionState;
  workspaceId: number;
  /** 日志里区分来源界面。 */
  source: "conversation" | "file";
}) {
  const bridge = useReferenceBridge();
  const [note, setNote] = useState("");
  const composing = useRef(false);
  const { pending, commenting } = state;
  const canAdd = !!bridge?.canAdd(workspaceId);

  useEffect(() => {
    if (!commenting || !pending?.range) return;
    CSS.highlights.set(PENDING_HIGHLIGHT, new Highlight(pending.range));
    return () => {
      CSS.highlights.delete(PENDING_HIGHLIGHT);
    };
  }, [commenting, pending]);

  const startComment = () => {
    setNote("");
    state.startComment();
  };
  useAppHotkey("addToChat", startComment, { enabled: !!pending && !commenting && canAdd });

  const submit = () => {
    if (!pending || !bridge) return;
    const text = note.trim();
    void bridge.add(workspaceId, [{ ...pending.quote, ...(text ? { note: text } : {}) }]);
    void frontendLog("INFO", "selection_quote_added", "摘录已加入输入框", {
      data: { source, length: pending.quote.text.length, withNote: Boolean(text), path: pending.quote.path },
    });
    window.getSelection()?.removeAllRanges();
    state.close();
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey || isImeComposing(event, composing.current)) return;
    event.preventDefault();
    submit();
  };

  if (!pending || !canAdd) return null;
  return (
    <Popover
      open
      onOpenChange={(open, details) => {
        if (open || !commenting) return;
        if (note.trim() && details.reason !== "escape-key") return;
        state.close();
      }}
    >
      <PopoverContent
        anchor={pending.anchor}
        side="bottom"
        align="end"
        initialFocus={commenting}
        finalFocus={false}
        className={commenting ? "w-80 gap-2 p-2.5" : "w-auto p-1"}
        data-slot="selection-toolbar"
      >
        {commenting ? (
          <>
            <div className="text-muted-foreground line-clamp-2 border-l-2 pl-2 text-xs">{quoteHeadline(pending.quote.text)}</div>
            <textarea
              aria-label="写批注"
              rows={2}
              value={note}
              placeholder="写批注（回车添加到输入框，Esc 取消）"
              className="placeholder:text-muted-foreground min-h-12 w-full resize-none bg-transparent text-sm outline-none"
              onChange={(event) => setNote(event.target.value)}
              onKeyDown={onKeyDown}
              onCompositionStart={() => { composing.current = true; }}
              onCompositionEnd={() => { composing.current = false; }}
            />
          </>
        ) : (
          <Button
            variant="ghost"
            size="sm"
            className="h-7 gap-1.5 px-2 text-xs"
            title={`摘录到对话（${shortcutLabel("addToChat")}）`}
            onMouseDown={(event) => event.preventDefault()}
            onClick={startComment}
          >
            <TextQuoteIcon className="size-3.5" />
            摘录
          </Button>
        )}
      </PopoverContent>
    </Popover>
  );
}
