import { useEffect, useRef, useState, type RefObject } from "react";
import { ChevronLeftIcon, ChevronRightIcon, FileSearchIcon, SquareIcon } from "lucide-react";

import { FindBar } from "@/components/ui/find-bar";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import type { TextViewerHandle } from "@/components/text-viewer/readonly-text-viewer";
import { parseFindTextBatch, type FileInfo, type FindTextBatch } from "@/lib/api/workspace-files";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import type { FindDirection, FindResult } from "@/lib/find/next-match";
import { useDomFind } from "@/lib/find/use-dom-find";
import { useReadingCommand } from "@/lib/workspace-files/reading-commands";
import { useStreamedSearch } from "./use-streamed-search";

/**
 * 文本阅读器的查找：就地查当前显示的内容，源码视图交给只读编辑器（`TextViewerHandle.find`），Markdown 渲染
 * 视图交给 DOM 高亮（`useDomFind`）。查找条与终端共用 `FindBar`，Ctrl/Cmd+F 打开或重新聚焦。
 *
 * 只有分段读取的大文件（`wholeFile` 为假）才需要后端：查找条多一个「在整个文件中查找」，结果列表点开对应
 * 分段（`onJump`）。整份加载的文件从不请求后端，也就不会出现「从匹配处截断、行号从 1 重新计」的跳转。
 */
export function useTextFind({ workspaceId, info, encoding, rendered, wholeFile, contentKey, editor, markdownHost, trigger, onJump }: {
  workspaceId: number;
  info: FileInfo;
  encoding: string;
  rendered: boolean;
  wholeFile: boolean;
  /** 显示的内容变了（换视图、换分段、换版本）就按当前词重查，高亮不停留在旧内容上。 */
  contentKey: string;
  editor: RefObject<TextViewerHandle | null>;
  markdownHost: RefObject<HTMLElement | null>;
  trigger: RefObject<HTMLButtonElement | null>;
  onJump: (offset: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const [focusKey, setFocusKey] = useState(0);
  const [result, setResult] = useState<FindResult | null>(null);
  const term = useRef("");
  const dom = useDomFind(markdownHost);
  const clearDom = dom.clear;
  const run = (next: string, direction: FindDirection) => {
    term.current = next;
    setResult(rendered ? dom.find(next, direction) : (editor.current?.find(next, direction) ?? null));
  };
  // 提交到后端的整文件查找；`generation` 让同一个词再查一次也从第一页开始。
  const [submitted, setSubmitted] = useState<{ query: string; generation: number } | null>(null);
  const search = useStreamedSearch({
    workspaceId,
    operation: "find-text",
    request: submitted && {
      body: { path: info.path, query: submitted.query, encoding, version: info.version },
      generation: submitted.generation,
    },
    decode: parseFindTextBatch,
    items: (batch: FindTextBatch) => batch.matches,
  });
  useReadingCommand("find", () => {
    setOpen(true);
    setFocusKey((key) => key + 1);
  });
  const latestRun = useRef(run);
  latestRun.current = run;
  useEffect(() => {
    if (!open || !term.current) return;
    clearDom();
    latestRun.current(term.current, "next");
  }, [open, contentKey, clearDom]);
  const close = () => {
    clearDom();
    editor.current?.find("", "next");
    term.current = "";
    setResult(null);
    setSubmitted(null);
    setOpen(false);
    trigger.current?.focus();
  };
  const status =
    search.phase === "running" ? "查找中…"
      : search.phase === "complete" ? "查找完成"
        : search.phase === "stopped" ? "已停止"
          : search.phase === "failed" ? userErrorMessage(search.error, "查找失败")
            : "";
  const overlay = open && (
    <div className="absolute top-2 right-4 z-10 flex max-w-[calc(100%-2rem)] flex-col items-end gap-1">
      <FindBar label="在文件中查找" result={result} onSearch={run} onClose={close} focusKey={focusKey}>
        {!wholeFile && (
          <TooltipIconButton tooltip="在整个文件中查找（含未加载的分段）" aria-label="在整个文件中查找" className="size-6"
            disabled={!term.current}
            onClick={() => setSubmitted((previous) => ({ query: term.current, generation: (previous?.generation ?? 0) + 1 }))}>
            <FileSearchIcon aria-hidden="true" />
          </TooltipIconButton>
        )}
      </FindBar>
      {submitted && (
        <div className="bg-popover text-popover-foreground w-80 max-w-full rounded-md border p-1 text-xs shadow-md" aria-label="整个文件的查找结果">
          <div className="flex items-center gap-1 px-1">
            <span role="status" className="text-muted-foreground min-w-0 flex-1 truncate">{status} · {search.items.length} 处</span>
            {search.phase === "running" && (
              <TooltipIconButton tooltip="停止" aria-label="停止" className="size-6" onClick={search.stop}><SquareIcon /></TooltipIconButton>
            )}
            {(search.hasPrevious || search.hasNext) && (
              <>
                <TooltipIconButton tooltip="上一页结果" aria-label="上一页结果" className="size-6" disabled={!search.hasPrevious} onClick={search.previousPage}><ChevronLeftIcon /></TooltipIconButton>
                <TooltipIconButton tooltip="下一页结果" aria-label="下一页结果" className="size-6" disabled={!search.hasNext} onClick={search.nextPage}><ChevronRightIcon /></TooltipIconButton>
              </>
            )}
          </div>
          <div className="max-h-48 overflow-auto">
            {search.items.map((match, index) => (
              <button
                key={index}
                type="button"
                className="block w-full truncate rounded px-2 py-1 text-left hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring"
                onClick={() => onJump(match.offset)}
              >
                {match.snippet}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
  return { open, toggle: () => (open ? close() : setOpen(true)), close, overlay };
}
