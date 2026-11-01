import {
  selectedReference,
  ReferenceSelectionMenu,
  type TextSelection,
} from "@/components/workspace-references/selection-actions";
import { AddToChatButton } from "./add-to-chat-button";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getText,
  type FileInfo,
} from "@/lib/api/workspace-files";
import { queryKeys } from "@/lib/query/query-keys";
import { fileSession, useFilesStore } from "@/lib/workspace-files/store";
import { HttpError } from "@/lib/http/errors";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import { Button } from "@/components/ui/button";
import { NativeSelect } from "@/components/ui/native-select";
import { SearchIcon } from "lucide-react";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { ReadingToolbar, type ReadingNavigation } from "./reading-toolbar";
import { useTextFind } from "./text-find";
import { OutlineButton } from "./outline-button";
import type { TextViewerHandle } from "@/components/text-viewer/readonly-text-viewer";
import { CodePreview } from "./code-preview";
import { MarkdownPreview } from "./markdown-preview";

/**
 * 文本类文件的阅读器：分段读取、全文件查找、编码与换行，源码用只读编辑器显示，Markdown 的渲染视图
 * （`rendered`）用同一份文本渲染。看渲染还是源码由 `FilePreview` 决定并传入，这里只在需要行的场合
 * （点击查找结果）把偏好切回源码。
 */
export function TextPreview({
  workspaceId,
  info,
  navigation,
  rendered,
  view,
}: {
  workspaceId: number;
  info: FileInfo;
  navigation: ReadingNavigation;
  rendered: boolean;
  view?: ReactNode;
}) {
  const [selection, setSelection] = useState<TextSelection | null>(null);
  const [excerpt, setExcerpt] = useState<string | null>(null);
  const position = useFilesStore(
    (s) => fileSession(s.sessions, workspaceId).positions[info.path],
  );
  const setPosition = useFilesStore((s) => s.position);
  const queryClient = useQueryClient();
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
  const searchTrigger = useRef<HTMLButtonElement>(null);
  const editor = useRef<TextViewerHandle>(null);
  const markdownHost = useRef<HTMLDivElement>(null);
  const previousVersion = useRef(info.version);
  useEffect(() => {
    if (previousVersion.current !== info.version) {
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
  // 带行号打开（`ReadingPosition.reveal`）：整份加载的文件在源码里选中并居中；分段读取的大文件没有
  // 行号到字节偏移的换算，只停在开头并说明。切到源码视图由 `FilePreview` 负责。
  const reveal = position?.reveal;
  const wholeFile = page.data?.offset === 0 && page.data.next_offset === null;
  const [revealSkipped, setRevealSkipped] = useState(false);
  useEffect(() => {
    if (reveal && page.data && !wholeFile) {
      setRevealSkipped(true);
      setPosition(workspaceId, info.path, { reveal: undefined });
    }
  }, [reveal, page.data, wholeFile, workspaceId, info.path, setPosition]);
  const find = useTextFind({
    workspaceId,
    info,
    encoding,
    rendered,
    wholeFile,
    contentKey: `${rendered}:${info.version}:${encoding}:${page.data?.offset}`,
    editor,
    markdownHost,
    trigger: searchTrigger,
    // 整文件查找的结果在别的分段里：切到源码、从命中处读起（只有分段读取的大文件会走到这里）。
    onJump: (next) => {
      if (rendered) setPreference(workspaceId, info.path, { view: "source" });
      go(next);
    },
  });
  const go = (next: number) => {
    setPosition(workspaceId, info.path, {
      offset: next,
      before: undefined,
      tail: false,
      scrollTop: 0,
    });
  };
  const source = page.data && (
    <CodePreview
      ref={editor}
      key={`${info.path}:${page.data.offset}`}
      text={page.data.text}
      name={info.name}
      onSelection={(value) => {
        setSelection(value && page.data.offset ? { ...value, startLine: undefined, endLine: undefined } : value);
        setExcerpt(null);
      }}
      wrap={wrap}
      reveal={wholeFile ? reveal : undefined}
      onRevealed={() => setPosition(workspaceId, info.path, { reveal: undefined })}
      scrollTop={position?.scrollTop ?? 0}
      onScroll={(scrollTop) =>
        setPosition(workspaceId, info.path, { scrollTop })
      }
    />
  );
  const markdown = page.data && (
    <MarkdownPreview
      hostRef={markdownHost}
      text={page.data.text}
      onSelectText={(value) => {
        setExcerpt(value);
        setSelection(null);
      }}
      workspaceId={workspaceId}
      path={info.path}
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
        if (event.key === "Escape" && find.open &&
          event.currentTarget.contains(event.target as Node) &&
          !(event.target as HTMLElement).closest('[aria-haspopup][aria-expanded="true"]')) {
          event.preventDefault();
          find.close();
        }
      }}
    >
      <ReadingToolbar navigation={navigation} info={info}
        view={<>
          {view}
          {info.kind === "markdown" && page.data && (
            <OutlineButton
              text={page.data.text}
              partial={!wholeFile}
              onSelect={({ line }) => {
                if (rendered)
                  markdownHost.current?.querySelector(`[data-source-line="${line}"]`)?.scrollIntoView({ block: "start" });
                else editor.current?.revealLine(line);
              }}
            />
          )}
        </>}
        actions={<>
          <TooltipIconButton ref={searchTrigger} tooltip="查找文件内容" aria-label="查找文件内容" className="size-8 shrink-0" aria-expanded={find.open}
            onClick={find.toggle}><SearchIcon /></TooltipIconButton>
          <AddToChatButton
            workspaceId={workspaceId}
            path={info.path}
            selection={selection}
            excerpt={excerpt}
          />
        </>}
        options={<>
          <NativeSelect
            aria-label="文本编码"
            size="sm"
            wrapperClassName="w-32"
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
          </NativeSelect>
          {!rendered && (
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
            {rendered
              ? "Ctrl/Cmd+F 查找 · Enter / Shift+Enter 下一处 / 上一处"
              : "Ctrl/Cmd+F 查找 · Alt+G 跳转到行"}
          </span>
        </>}
      />
      {/* 查找浮层挂在不随读取状态变化的容器上：换编码、换分段时查找条与输入的词都保留。 */}
      <div className="relative flex min-h-0 min-w-0 flex-1 flex-col">
        {find.overlay}
        {page.error ? (
          <ErrorState
            className="m-4"
            message={userErrorMessage(page.error, "文本读取失败")}
            onRetry={() => void page.refetch()}
          />
        ) : page.isPending ? (
          <LoadingState label="正在读取文件…" />
        ) : (
          <ReferenceSelectionMenu
            workspaceId={workspaceId}
            input={selectedReference(info.path, selection, excerpt)}
          >
            <div className="min-h-0 min-w-0 flex-1">{rendered ? markdown : source}</div>
          </ReferenceSelectionMenu>
        )}
      </div>
      {info.size > 2 * 1024 * 1024 && (
        <div className="flex flex-wrap items-center gap-1 shrink-0 border-t px-2 py-1 text-xs">
          <span>
            分段阅读 · {page.data?.offset ?? offset} / {info.size} 字节
          </span>
          {revealSkipped && (
            <span className="text-muted-foreground">· 大文件不支持按行定位，已打开开头</span>
          )}
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
