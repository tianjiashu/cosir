import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/state-panel";
import { PanelResizeHandle } from "@/components/ui/panel-resize-handle";
import type { ContextMessage } from "@/lib/api/context-messages";
import { MessageList } from "./message-list";
import { MessageDetail } from "./message-detail";
import { useMessageNavigation } from "./use-message-navigation";

export function ObserverResults({
  messages,
  initialSequence,
  onWholeTask,
}: {
  messages: ContextMessage[];
  initialSequence?: number;
  onWholeTask?: (sequence?: number) => void;
}) {
  const nav = useMessageNavigation(messages, initialSequence);
  const host = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [preferredWidth, setPreferredWidth] = useState<number | null>(null);
  const previous = useRef(new Set(messages.map((message) => message.sequence)));
  const [newSequences, setNewSequences] = useState<number[]>([]);
  useEffect(() => {
    const observer = new ResizeObserver((entries) =>
      setWidth(entries[0].contentRect.width),
    );
    observer.observe(host.current!);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    const current = new Set(messages.map((message) => message.sequence));
    const added = messages
      .filter((message) => !previous.current.has(message.sequence))
      .map((message) => message.sequence);
    previous.current = current;
    setNewSequences((sequences) =>
      [...new Set([...sequences, ...added])].filter((sequence) =>
        current.has(sequence),
      ),
    );
  }, [messages]);
  const narrow = width < 900;
  const listWidth = Math.max(
    300,
    Math.min(width - 420, preferredWidth ?? width * 0.4),
  );
  const listVisible = !narrow || !nav.detailOpen || !nav.selected;
  const backToList = () => {
    nav.setDetailOpen(false);
    requestAnimationFrame(() =>
      host.current
        ?.querySelector<HTMLElement>('[role="listbox"]')
        ?.focus({ preventScroll: true }),
    );
  };
  useEffect(() => {
    if (narrow && nav.detailOpen && nav.selected)
      host.current
        ?.querySelector<HTMLElement>('[aria-label="消息详情"] button')
        ?.focus();
  }, [narrow, nav.detailOpen, nav.selected?.sequence]);
  return (
    <div ref={host} className="flex min-h-0 flex-1 flex-col">
      {(onWholeTask || newSequences.length > 0) && (
        <div className="flex shrink-0 flex-wrap items-center gap-2 border-b px-4 py-1.5">
          {onWholeTask && (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => onWholeTask(nav.selected?.sequence)}
            >
              查看整个任务
            </Button>
          )}
          {newSequences.length > 0 && (
            <Button
              size="sm"
              variant="outline"
              className="ml-auto"
              onClick={() => {
                nav.locate(Math.max(...newSequences));
                setNewSequences([]);
              }}
            >
              新增 {newSequences.length} 条 · 查看最新
            </Button>
          )}
        </div>
      )}
      {!messages.length ? (
        <EmptyState
          className="m-6 border-0"
          title="该范围暂无消息记录"
          description="执行产生消息后，可通过上方刷新重新读取。"
        />
      ) : (
        <div className="flex min-h-0 flex-1">
          <div
            className={listVisible ? "min-h-0 shrink-0" : "hidden"}
            style={{ width: narrow ? "100%" : listWidth }}
          >
            <MessageList
              messages={messages}
              navigation={nav}
              visible={listVisible}
            />
          </div>
          {!narrow && (
            <PanelResizeHandle
              label="调整消息列表宽度"
              value={listWidth}
              min={300}
              max={Math.max(300, width - 420)}
              onChange={setPreferredWidth}
            />
          )}
          <div
            className={
              narrow && listVisible ? "hidden" : "min-h-0 min-w-0 flex-1"
            }
          >
            {nav.selected ? (
              <MessageDetail
                message={nav.selected}
                messages={messages}
                onLocate={nav.locate}
                onBack={backToList}
                narrow={narrow}
              />
            ) : (
              <div className="flex h-full items-center justify-center p-6 text-sm text-muted-foreground">
                调整筛选后选择一条消息，查看内容与结构。
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
