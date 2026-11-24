import { useBrowserVisibility } from "@/lib/workspace-files/visibility";
import { useEffect, useMemo, useRef, useState } from "react";
import type { Editor } from "@tiptap/react";
import { parseSearchBatch, type SearchBatch, type FileEntry } from "@/lib/api/workspace-files";
import { frontendLog } from "@/lib/logging/frontend-log";
import { useStreamedSearch } from "@/lib/workspace-files/use-streamed-search";
import { useReferenceBridge } from "./reference-bridge";
import { workspaceDirectoryRef, workspaceFileRef } from "@/lib/workspace-references/ref-capsule";
import { WorkspaceFileIcon } from "@/components/workspace-files/file-icon";
import { Popover, PopoverContent } from "@/components/ui/popover";
import { parentPath } from "@/lib/workspace-files/workspace-files-store";

/**
 * 输入框里的 @ 引用：用编辑器的真实选区识别 @；输入法组合输入期间不提交候选，也不抢 Enter 发送。
 *
 * 候选浮在整个输入框上方（锚点是输入框外框 `data-slot="composer-surface"`，宽度跟随它），外观与「+」菜单
 * （`ComposerMenuContent` / `ComposerMenuRow`）同一套；但不能共用那个组件：它打开时把焦点交给 cmdk，
 * 这里焦点必须留在编辑器里继续打字，键盘由下面挂在编辑器上的捕获监听处理。
 * 一行只有图标、名称与淡色的所在目录；没有说明行，空结果、搜索中、失败各只占一行淡色文字。
 *
 * 搜索走与文件面板同一个 `useStreamedSearch`（阶段明确：进行中 / 完成 / 失败），候选边搜边显示。页面上能选的
 * 候选只在 `visible` 算一次，渲染与键盘都读它：曾经用一个状态字符串同时表示「正在搜」与「没东西可显示」，
 * 搜索未完成时候选被藏起来、回车却选中了看不见的文件。
 */
export function ReferenceMention({
  editor,
  bridge,
  target,
}: {
  editor: Editor | null;
  bridge: ReturnType<typeof useReferenceBridge>;
  target: string | null;
}) {
  const [mention, setMention] = useState<{
    from: number;
    to: number;
    query: string;
  } | null>(null);
  const [index, setIndex] = useState(0);
  const dismissed = useRef<string | null>(null);
  useEffect(() => {
    if (!editor) return;
    const update = () => {
      if (!editor.isFocused || !editor.state.selection.empty) {
        setMention(null);
        return;
      }
      const { $from } = editor.state.selection;
      const prefix = $from.parent.textBetween(
        0,
        $from.parentOffset,
        "\n",
        "\ufffc",
      );
      const match = /(?:^|\s)@([^\s@]*)$/.exec(prefix);
      const signature = `${$from.pos}:${prefix}`;
      // Esc 仅关闭当前输入；内容或光标改变后，相同路径可以再次发起候选查询。
      if (dismissed.current !== signature) dismissed.current = null;
      if (!match || dismissed.current === signature || editor.view.composing) {
        setMention(null);
        return;
      }
      setMention({
        from: $from.pos - match[1].length - 1,
        to: $from.pos,
        query: match[1],
      });
    };
    editor.on("selectionUpdate", update);
    editor.on("update", update);
    return () => {
      editor.off("selectionUpdate", update);
      editor.off("update", update);
    };
  }, [editor]);
  const query = mention?.query;
  const visibility = useBrowserVisibility(
    bridge?.workspaceId ?? 0,
    query !== undefined && !!bridge?.workspaceId,
  );
  const revision = visibility.data?.revision;
  const workspaceId = bridge?.workspaceId ?? null;
  const searching = query !== undefined && workspaceId !== null && bridge?.target === target;
  const search = useStreamedSearch({
    workspaceId: workspaceId ?? 0,
    operation: "search",
    // 浏览可见性配置变了（忽略或恢复了文件）就重新搜一次。
    // 请求就是当前输入：打字停稳 200ms 才发出，期间上一个查询的结果已经作废（hook 只交出当前请求的结果）。
    request: searching ? { body: { query: query || "*", include_directories: true }, generation: revision } : null,
    delayMs: 200,
    decode: parseSearchBatch,
    items: (batch: SearchBatch) => batch.entries,
    onError: (error) => {
      void frontendLog("WARNING", "workspace_reference_search_failed", "@ 引用候选搜索失败", { data: { workspaceId }, error });
    },
  });
  // 超过 100 条不另提示：继续输入即可缩小范围。
  const visible = useMemo(
    () => search.items.filter((entry) => entry.kind !== "unavailable").slice(0, 100),
    [search.items],
  );
  const requestKey = searching ? `${query}:${revision}` : null;
  useEffect(() => setIndex(0), [requestKey]);
  const choose = (entry: FileEntry) => {
    if (!editor || !mention || !bridge?.workspaceId) return;
    // 先删掉已输入的 `@query`：胶囊插入的是当前光标处，正好落在原来的 `@` 位置。
    editor.chain().focus().deleteRange({ from: mention.from, to: mention.to }).run();
    void bridge.add(bridge.workspaceId, [
      entry.kind === "directory" ? workspaceDirectoryRef(entry.path) : workspaceFileRef(entry.path),
    ]);
  };
  // Esc 与点到列表外都只关闭这一次输入的候选；内容或光标再变时重新查询。
  const dismiss = () => {
    if (!editor) return;
    const { $from } = editor.state.selection;
    dismissed.current = `${$from.pos}:${$from.parent.textBetween(0, $from.parentOffset, "\n", "\ufffc")}`;
    setMention(null);
  };
  useEffect(() => {
    if (!editor || !mention || bridge?.target !== target) return;
    const keydown = (event: KeyboardEvent) => {
      if (event.isComposing || editor.view.composing) return;
      if (!["ArrowDown", "ArrowUp", "Enter", "Escape"].includes(event.key))
        return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (event.key === "Escape") dismiss();
      else if (event.key === "Enter") {
        if (visible[index]) choose(visible[index]);
      } else
        setIndex((current) =>
          Math.max(
            0,
            Math.min(
              visible.length - 1,
              current + (event.key === "ArrowDown" ? 1 : -1),
            ),
          ),
        );
    };
    const dom = editor.view.dom;
    dom.addEventListener("keydown", keydown, true);
    return () => dom.removeEventListener("keydown", keydown, true);
  });
  if (!editor || !mention || !bridge?.workspaceId || bridge.target !== target) return null;
  // 有候选就显示候选；没有时按搜索阶段说一句。
  const placeholder = visible.length
    ? ""
    : search.phase === "failed"
      ? "搜索失败，请重新输入"
      : search.phase === "complete"
        ? "没有匹配项"
        : "搜索中…";
  return (
    <Popover open onOpenChange={(open) => { if (!open) dismiss(); }}>
      <PopoverContent
        anchor={editor.view.dom.closest('[data-slot="composer-surface"]')}
        side="top"
        align="start"
        sideOffset={6}
        initialFocus={false}
        finalFocus={false}
        className="w-(--anchor-width) gap-0 rounded-xl p-1 shadow-lg"
      >
        <div className="max-h-80 overflow-y-auto" role="listbox" aria-label="工作区引用候选">
          {placeholder ? (
            <p className="text-muted-foreground px-2.5 py-2 text-sm">{placeholder}</p>
          ) : visible.map((entry, i) => {
            const directory = parentPath(entry.path);
            return (
              <button
                type="button"
                role="option"
                aria-selected={i === index}
                aria-label={entry.path}
                key={entry.path}
                className={`flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-sm ${i === index ? "bg-muted" : ""}`}
                onMouseDown={(e) => e.preventDefault()}
                onMouseMove={() => setIndex(i)}
                ref={(element) => {
                  if (i === index) element?.scrollIntoView({ block: "nearest" });
                }}
                onClick={() => choose(entry)}
              >
                <WorkspaceFileIcon name={entry.name} folder={entry.kind === "directory"} />
                <span className="shrink-0 truncate">{entry.name}</span>
                {directory && <span className="text-muted-foreground min-w-0 truncate text-xs">{directory}</span>}
              </button>
            );
          })}
        </div>
      </PopoverContent>
    </Popover>
  );
}
