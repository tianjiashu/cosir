import { CornerDownLeftIcon, TextQuoteIcon } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";

import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import { Popover, PopoverContent } from "@/components/ui/popover";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { useReferenceBridge } from "@/components/workspace-references/reference-bridge";
import { quoteHeadline } from "@/lib/assistant/capsules";
import { isImeComposing } from "@/lib/keyboard/ime";
import { shortcutLabel, useAppHotkey } from "@/lib/keyboard/shortcuts";
import { frontendLog } from "@/lib/logging/frontend-log";

import type { TextSelectionState } from "./use-text-selection";

/** 评论框打开期间，原选区的替身高亮（焦点进了评论框，页面上的原选区会消失）。 */
const PENDING_HIGHLIGHT = "cosir-selection-pending";

/**
 * 「选中 → 对话」的浮条：选区确定后在选区第一行上方、与选区起点对齐出现「摘录」（放在下方会盖住正往下读的内容，上方放不下时
 * 由弹层自动翻到下方）；点它或按 Cmd+L 在原位展开评论框，回车或发送按钮把摘录连同批注
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
        side="top"
        align="start"
        sideOffset={8}
        initialFocus={commenting}
        finalFocus={false}
        className={commenting ? "w-96 gap-2.5 rounded-2xl p-3 shadow-lg" : "w-auto flex-row items-center gap-0.5 rounded-2xl p-1 shadow-lg"}
        data-slot="selection-toolbar"
      >
        {commenting ? (
          <>
            <div className="text-muted-foreground line-clamp-2 border-l-2 pl-2.5 text-xs leading-relaxed">{quoteHeadline(pending.quote.text)}</div>
            <div className="flex items-end gap-2">
              {/* 随内容长高，到约 5 行后滚动；回车提交、Esc 关闭是通用约定，不写进提示。 */}
              <textarea
                aria-label="写批注"
                rows={1}
                value={note}
                placeholder="写批注（可留空）"
                className="placeholder:text-muted-foreground field-sizing-content max-h-32 min-h-8 flex-1 resize-none bg-transparent py-1.5 text-sm outline-none"
                onChange={(event) => setNote(event.target.value)}
                onKeyDown={onKeyDown}
                onCompositionStart={() => { composing.current = true; }}
                onCompositionEnd={() => { composing.current = false; }}
              />
              {/* 批注可留空：只加摘录也是一次完整的添加，按钮始终可用。图标用回车符（与回车提交一致）、浅色样式：
                  深色圆形上箭头只代表输入框的「发给 Agent」，这里只是放进输入框，不能长得一样。 */}
              <TooltipIconButton tooltip="添加到输入框（↵）" aria-label="添加到输入框" variant="secondary" className="size-8 shrink-0" onClick={submit}>
                <CornerDownLeftIcon />
              </TooltipIconButton>
            </div>
          </>
        ) : (
          <>
            <Button
              variant="ghost"
              size="sm"
              className="h-7 gap-1.5 rounded-xl px-2.5 text-xs"
              onMouseDown={(event) => event.preventDefault()}
              onClick={startComment}
            >
              <TextQuoteIcon className="size-3.5" />
              摘录
            </Button>
            <Kbd className="mr-1 bg-transparent">{shortcutLabel("addToChat")}</Kbd>
          </>
        )}
      </PopoverContent>
    </Popover>
  );
}
