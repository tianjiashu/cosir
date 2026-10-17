import {
  SelectionActions,
  selectedReference,
  ReferenceSelectionMenu,
  type TextSelection,
} from "@/components/workspace-references/selection-actions";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { ReadingSearchContext } from "@/lib/workspace-files/reading-search";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getText,
  readFileStream,
  type FileInfo,
} from "@/lib/api/workspace-files";
import { queryKeys } from "@/lib/query/query-keys";
import { fileSession, useFilesStore } from "@/lib/workspace-files/store";
import { HttpError } from "@/lib/http/errors";
import { Button } from "@/components/ui/button";
import { SearchIcon } from "lucide-react";
import { TooltipIconButton } from "@/components/tooltip-icon-button";
import { ReadingToolbar, type ReadingNavigation } from "./reading-toolbar";
import { CodePreview } from "./code-preview";
import { MarkdownPreview } from "./markdown-preview";

type Match = { offset: number; snippet: string; };
export function TextPreview({
  workspaceId,
  info,
  navigation,
  view,
}: {
  workspaceId: number;
  info: FileInfo;
  navigation: ReadingNavigation;
  view?: ReactNode;
}) {
  const [selection, setSelection] = useState<TextSelection | null>(null);
  const [excerpt, setExcerpt] = useState<string | null>(null);
  const position = useFilesStore(
    (s) => fileSession(s.sessions, workspaceId).positions[info.path],
  );
  const setPosition = useFilesStore((s) => s.position);
  const open = useFilesStore((s) => s.open);
  const queryClient = useQueryClient();
  const openDocument = useCallback(
    (path: string) => open(workspaceId, path),
    [open, workspaceId],
  );
  const offset = position?.offset ?? 0;
  const encoding = position?.encoding ?? info.encoding ?? "utf-8";
  const tail = position?.tail ?? false;
  const before = position?.before;
  const setTail = (value: boolean) =>
    setPosition(workspaceId, info.path, { tail: value, before: undefined });
  const preference = useFilesStore(
    (s) => fileSession(s.sessions, workspaceId).preferences[info.path],
  );
  const setPreference = useFilesStore((s) => s.preference);
  const wrap = preference?.wrap ?? false;
  const mode =
    preference?.mode ?? (info.kind === "markdown" ? "preview" : "source");
  const setMode = (mode: "source" | "preview" | "split") =>
    setPreference(workspaceId, info.path, { mode });
  const [showSearch, setShowSearch] = useState(false);
  const searchTrigger = useRef<HTMLButtonElement>(null);
  const closeSearch = () => {
    search.current?.abort();
    setSearching(false);
    setShowSearch(false);
    setSearchStatus("");
    searchTrigger.current?.focus();
  };
  const searchInput = useRef<HTMLInputElement>(null);
  const registerSearch = useContext(ReadingSearchContext)!;
  useEffect(() => {
    registerSearch(() => {
      setShowSearch(true);
      searchInput.current?.focus();
    });
    return () => registerSearch(null);
  }, [registerSearch]);
  useEffect(() => {
    if (showSearch) searchInput.current?.focus();
  }, [showSearch]);
  const [searching, setSearching] = useState(false);
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<Match[]>([]);
  const [searchStatus, setSearchStatus] = useState("");
  const [matchOffset, setMatchOffset] = useState(0);
  const [nextMatchOffset, setNextMatchOffset] = useState<number | null>(null);
  const search = useRef<AbortController | null>(null);
  useEffect(() => () => search.current?.abort(), []);
  useEffect(() => {
    search.current?.abort();
    setMatches([]);
    setSearchStatus("");
    setSearching(false);
    setMatchOffset(0);
    setNextMatchOffset(null);
  }, [encoding]);
  const previousVersion = useRef(info.version);
  useEffect(() => {
    if (previousVersion.current !== info.version) {
      search.current?.abort();
      setMatches([]);
      setSearchStatus("");
      setSearching(false);
      setPosition(workspaceId, info.path, {
        offset: 0,
        tail: false,
        before: undefined,
      });
      previousVersion.current = info.version;
    }
  }, [info.version, info.path, workspaceId, offset, setPosition]);
  const page = useQuery({
    queryKey: queryKeys.fileText(
      workspaceId,
      info.path,
      info.version,
      encoding,
      offset,
      tail,
      before,
    ),
    queryFn: ({ signal }) =>
      getText(
        workspaceId,
        info.path,
        offset,
        encoding,
        info.version,
        tail,
        before,
        signal,
      ),
    gcTime: 30_000,
  });
  useEffect(() => {
    setSelection(null);
    setExcerpt(null);
  }, [info.version, encoding, offset, tail, before]);
  useEffect(() => {
    if (page.error instanceof HttpError && page.error.status === 409) {
      setPosition(workspaceId, info.path, { offset: 0 });
      void queryClient.invalidateQueries({
        queryKey: queryKeys.fileInfo(workspaceId, info.path),
      });
    }
  }, [page.error, workspaceId, info.path, queryClient, setPosition]);
  const go = (next: number) => {
    setPosition(workspaceId, info.path, {
      offset: next,
      before: undefined,
      tail: false,
      scrollTop: 0,
    });
  };
  const find = async (skip = 0) => {
    setMatchOffset(skip);
    setNextMatchOffset(null);
    search.current?.abort();
    if (!query) {
      setSearching(false);
      return;
    }
    const controller = new AbortController();
    search.current = controller;
    setMatches([]);
    setSearching(true);
    setSearchStatus("查找中…");
    try {
      await readFileStream<{
        matches: Match[];
        complete: boolean;
        next_offset?: number | null;
        error?: string;
      }>(
        workspaceId,
        "find-text",
        {
          path: info.path,
          query,
          encoding,
          version: info.version,
          offset: skip,
        },
        controller.signal,
        (batch) => {
          if (controller.signal.aborted) return;
          if (batch.error) throw new Error(batch.error);
          setMatches((current) => [...current, ...batch.matches]);
          if (batch.complete) {
            setSearching(false);
            setSearchStatus("查找完成");
            setNextMatchOffset(batch.next_offset ?? null);
          }
        },
      );
    } catch (error) {
      if (!controller.signal.aborted) {
        setSearching(false);
        setSearchStatus(userErrorMessage(error, "查找失败"));
      }
    }
  };
  const source = page.data && (
    <CodePreview
      key={`${info.path}:${page.data.offset}`}
      text={page.data.text}
      name={info.name}
      onSelection={(value) => {
        setSelection(value && page.data.offset ? { ...value, startLine: undefined, endLine: undefined } : value);
        setExcerpt(null);
      }}
      wrap={wrap}
      scrollTop={position?.scrollTop ?? 0}
      onScroll={(scrollTop) =>
        setPosition(workspaceId, info.path, { scrollTop })
      }
    />
  );
  const rendered = page.data && (
    <MarkdownPreview
      text={page.data.text}
      onSelectText={(value) => {
        setExcerpt(value);
        setSelection(null);
      }}
      workspaceId={workspaceId}
      path={info.path}
      open={openDocument}
      scrollTop={position?.previewScrollTop ?? 0}
      onScroll={(previewScrollTop) =>
        setPosition(workspaceId, info.path, { previewScrollTop })
      }
    />
  );
  return (
    <div
      className="flex h-full min-h-0 flex-col"
      onKeyDownCapture={(event) => {
        // 弹出层及其仍持有焦点的触发器先处理 Escape，阅读器其它区域保持原有收起查找行为。
        if (event.key === "Escape" && showSearch &&
          event.currentTarget.contains(event.target as Node) &&
          !(event.target as HTMLElement).closest('[aria-haspopup][aria-expanded="true"]')) {
          event.preventDefault();
          closeSearch();
        }
      }}
    >
      <ReadingToolbar navigation={navigation} info={info}
        view={<>{view}        {info.kind === "markdown" && (
          <select
            aria-label="Markdown 视图"
            className="rounded border bg-background p-1"
            value={mode}
            onChange={(e) => setMode(e.target.value as typeof mode)}
          >
            <option value="source">源码</option>
            <option value="preview">预览</option>
            <option value="split">分栏</option>
          </select>
        )}
        </>}
        actions={<>
          <TooltipIconButton ref={searchTrigger} tooltip="查找文件内容" aria-label="查找文件内容" className="size-8 shrink-0" aria-expanded={showSearch}
            onClick={() => showSearch ? closeSearch() : setShowSearch(true)}><SearchIcon /></TooltipIconButton>
          <SelectionActions workspaceId={workspaceId} path={info.path} encoding={encoding} selection={selection} excerpt={excerpt} />
        </>}
        options={<>
          <select
            aria-label="文本编码"
            className="max-w-28 rounded border bg-background p-1"
            value={encoding}
            onChange={(event) => {
              setTail(false);
              setPosition(workspaceId, info.path, {
                encoding: event.target.value,
                offset: 0,
              });
            }}
          >
            {[
              ...new Set([
                encoding,
                "utf-8",
                "utf-8-sig",
                "utf-16-le",
                "utf-16-be",
                "utf-32-le",
                "utf-32-be",
                "gb18030",
                "big5",
                "shift_jis",
                "cp1252",
              ]),
            ].map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
          {mode !== "preview" && (
            <Button
              size="sm"
              variant="ghost"
              aria-pressed={wrap}
              onClick={() =>
                setPreference(workspaceId, info.path, { wrap: !wrap })
              }
            >
              自动换行
            </Button>
          )}
          <span className="text-muted-foreground">
            {mode === "preview"
              ? "Ctrl/Cmd+F 查找文件内容"
              : "Ctrl/Cmd+F 全文件查找 · Alt+G 跳转当前段行"}
          </span>

        </>}
      />
      {showSearch && (
        <div className="border-b p-2 text-xs">
          <p className="mb-1 text-muted-foreground">
            查找整个文件（包括未加载的分段），结果打开对应源码片段。
          </p>
          <div className="flex items-center gap-1">
            <input
              ref={searchInput}
              aria-label="全文件查找"
              placeholder="全文件查找"
              className="min-w-0 flex-1 rounded border bg-background px-2 py-1"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void find();
              }}
            />
            <Button
              size="sm"
              variant="ghost"
              disabled={!query.trim()}
              onClick={() => void find()}
            >
              查找
            </Button>
            {searching && (
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  search.current?.abort();
                  setSearching(false);
                  setSearchStatus("已停止");
                }}
              >
                停止
              </Button>
            )}
            <Button
              size="sm"
              variant="ghost"
              onClick={closeSearch}
            >
              收起查找
            </Button>
          </div>
        </div>
      )}
      {showSearch && searchStatus && (
        <div className="max-h-36 shrink-0 overflow-auto border-b px-2 text-xs">
          <p>
            {searchStatus} · 本页 {matches.length} 处匹配{" "}
            <button
              onClick={() => {
                search.current?.abort();
                setSearching(false);
                setMatches([]);
                setSearchStatus("");
              }}
            >
              收起
            </button>
            {(matchOffset > 0 || nextMatchOffset !== null) && (
              <>
                <button
                  className="min-h-7 px-2"
                  disabled={matchOffset === 0}
                  onClick={() => void find(Math.max(0, matchOffset - 500))}
                >
                  上一页
                </button>{" "}
                <button
                  disabled={nextMatchOffset === null}
                  onClick={() => void find(nextMatchOffset!)}
                >
                  下一页
                </button>
              </>
            )}
          </p>
          {matches.map((match, index) => (
            <button
              key={index}
              className="block w-full truncate py-1 text-left hover:bg-muted"
              onClick={() => {
                setMode("source");
                go(match.offset);
              }}
            >
              {match.snippet}
            </button>
          ))}
        </div>
      )}
      {page.error ? (
        <div role="alert" className="p-4 text-sm">
          {userErrorMessage(page.error, "文本读取失败")}
          <Button variant="ghost" onClick={() => void page.refetch()}>
            重试
          </Button>
        </div>
      ) : page.isPending ? (
        <p className="p-4 text-sm">正在读取文件…</p>
      ) : (
        <ReferenceSelectionMenu
          workspaceId={workspaceId}
          input={selectedReference(info.path, selection, excerpt)}
        >
          <div className="flex min-h-0 flex-1">
            {mode !== "preview" && (
              <div className="min-h-0 min-w-0 flex-1">{source}</div>
            )}
            {mode !== "source" && (
              <div className="min-h-0 min-w-0 flex-1 border-l">{rendered}</div>
            )}
          </div>
        </ReferenceSelectionMenu>
      )}
      {info.size > 2 * 1024 * 1024 && (
        <div className="flex flex-wrap items-center gap-1 shrink-0 border-t px-2 py-1 text-xs">
          <span>
            分段阅读 · {page.data?.offset ?? offset} / {info.size} 字节
          </span>
          <Button size="sm" variant="ghost" onClick={() => go(0)}>
            开头
          </Button>
          <Button
            size="sm"
            variant="ghost"
            disabled={!page.data?.offset}
            onClick={() =>
              setPosition(workspaceId, info.path, {
                before: page.data!.offset,
                tail: false,
                scrollTop: 0,
              })
            }
          >
            上一段
          </Button>
          <Button
            size="sm"
            variant="ghost"
            disabled={page.data?.next_offset == null}
            onClick={() => go(page.data!.next_offset!)}
          >
            下一段
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setTail(true)}>
            末尾
          </Button>
        </div>
      )}
    </div>
  );
}
