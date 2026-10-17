import { useBrowserVisibility } from "@/lib/workspace-files/visibility";
import { useEffect, useRef, useState } from "react";
import type { Editor } from "@tiptap/react";
import {
  readFileStream,
  parseSearchBatch,
  type SearchBatch,
  type FileEntry,
} from "@/lib/api/workspace-files";
import { useReferenceBridge } from "./reference-bridge";
import { WorkspaceFileIcon } from "@/components/workspace-files/file-icon";

/** 使用编辑器的真实选区识别 @；输入法组合输入期间不提交候选，也不抢 Enter 发送。 */
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
  const [entries, setEntries] = useState<FileEntry[]>([]);
  const [index, setIndex] = useState(0);
  const [status, setStatus] = useState("");
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
  useEffect(() => {
    if (query === undefined || !bridge?.workspaceId || bridge.target !== target)
      return;
    const controller = new AbortController();
    setEntries([]);
    setIndex(0);
    setStatus("搜索中…");
    const timer = setTimeout(() => {
      void readFileStream<SearchBatch>(
        bridge.workspaceId!,
        "search",
        {
          query: query || "*",
          offset: 0,
          include_directories: true,
        },
        controller.signal,
        (batch) => {
          if (controller.signal.aborted) return;
          if (batch.error) throw new Error(batch.error);
          setEntries((previous) =>
            [...previous, ...batch.entries]
              .filter((entry) => entry.kind !== "unavailable")
              .slice(0, 100),
          );
          if (batch.complete)
            setStatus(
              batch.next_offset != null ? "结果较多，请输入更完整的路径" : "",
            );
        },
        parseSearchBatch,
      ).catch(() => {
        if (!controller.signal.aborted) setStatus("搜索失败，请重新输入");
      });
    }, 200);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [query, bridge?.workspaceId, bridge?.target, target, revision]);
  const choose = (entry: FileEntry) => {
    if (!editor || !mention || !bridge?.workspaceId) return;
    if (bridge.busy) return;
    const replacement = {
      from: mention.from,
      to: mention.to,
      text: `@${mention.query}`,
    };
    void bridge.add(
      bridge.workspaceId,
      [
        {
          path: entry.path,
          kind: entry.kind === "directory" ? "directory" : "file",
        },
      ],
      undefined,
      replacement,
    );
  };
  useEffect(() => {
    if (!editor || !mention || bridge?.target !== target) return;
    const keydown = (event: KeyboardEvent) => {
      if (event.isComposing || editor.view.composing) return;
      if (!["ArrowDown", "ArrowUp", "Enter", "Escape"].includes(event.key))
        return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (event.key === "Escape") {
        const { $from } = editor.state.selection;
        dismissed.current = `${$from.pos}:${$from.parent.textBetween(0, $from.parentOffset, "\n", "\ufffc")}`;
        setMention(null);
      } else if (event.key === "Enter") {
        if (entries[index]) choose(entries[index]);
      } else
        setIndex((current) =>
          Math.max(
            0,
            Math.min(
              entries.length - 1,
              current + (event.key === "ArrowDown" ? 1 : -1),
            ),
          ),
        );
    };
    const dom = editor.view.dom;
    dom.addEventListener("keydown", keydown, true);
    return () => dom.removeEventListener("keydown", keydown, true);
  });
  if (!mention || !bridge?.workspaceId || bridge.target !== target) return null;
  return (
    <div
      className="max-h-48 overflow-auto rounded-md border bg-popover p-1 text-sm"
      role="listbox"
      aria-label="工作区引用候选"
    >
      <p className="px-2 py-1 text-xs text-muted-foreground">
        {status ||
          (entries.length ? "选择文件或目录 · Esc 关闭" : "没有匹配项")}
      </p>
      {entries.map((entry, i) => (
        <button
          type="button"
          role="option"
          disabled={bridge.busy}
          aria-selected={i === index}
          key={entry.path}
          className={`flex w-full items-center gap-2 rounded px-2 py-1.5 text-left ${i === index ? "bg-muted" : ""}`}
          onMouseDown={(e) => e.preventDefault()}
          ref={(element) => {
            if (i === index) element?.scrollIntoView({ block: "nearest" });
          }}
          onClick={() => choose(entry)}
        >
          <WorkspaceFileIcon
            name={entry.name}
            folder={entry.kind === "directory"}
          />
          <span className="truncate">{entry.path}</span>
        </button>
      ))}
    </div>
  );
}
