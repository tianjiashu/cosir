import { ReferenceSelectionMenu } from "@/components/workspace-references/selection-actions";
import type { DomTextSelection } from "@/lib/workspace-files/dom-selection-locator";
import { sourceLinesOfRange } from "@/lib/workspace-files/dom-selection-locator";
import { workspaceFileRef } from "@/lib/workspace-references/ref-capsule";
import type { RefCapsule } from "@/lib/assistant/capsules";
import { AddToChatButton } from "./add-to-chat-button";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { useEffect, useRef, useState, type ReactNode } from "react";
import type { FileInfo } from "@/lib/api/workspace-files";
import { workspaceFiles, useFilesStore } from "@/lib/workspace-files/workspace-files-store";
import { useTextPage } from "@/lib/workspace-files/use-text-page";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import { Button } from "@/components/ui/button";
import { EncodingSelect } from "./encoding-select";
import { SearchIcon } from "lucide-react";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { ReadingToolbar, type ReadingNavigation } from "./reading-toolbar";
import { useTextFind } from "./text-find";
import { OutlineButton, revealSourceLine } from "@/components/markdown/outline-button";
import type { TextViewerHandle } from "@/components/text-viewer/readonly-text-viewer";
import { CodePreview } from "./code-preview";
import { MarkdownPreview } from "./markdown-preview";
import { mermaidDocument } from "@/lib/workspace-files/mermaid-document";

/**
 * 文本类文件的阅读器：分段读取（`useTextPage`）、全文件查找、编码与换行，源码用只读编辑器显示。渲染视图（`rendered`）
 * 用同一份文本按类型得出：Markdown 与 Mermaid（包成一个 mermaid 代码块）交给 Markdown 渲染；表格的渲染视图是
 * `TableReader`，不在这里。看渲染还是源码由 `FilePreview` 决定并传入，这里只在点击别的分段的查找结果时把偏好切回源码。
 * 只有 Markdown 有大纲。
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
  const [selection, setSelection] = useState<RefCapsule | null>(null);
  const { page, position, offset, encoding, tail, before, go, setEncoding, setPosition } = useTextPage(workspaceId, info);
  const preference = useFilesStore(
    (s) => workspaceFiles(s.byWorkspace, workspaceId).preferences[info.path],
  );
  const setPreference = useFilesStore((s) => s.preference);
  const wrap = preference?.wrap ?? false;
  const searchTrigger = useRef<HTMLButtonElement>(null);
  const editor = useRef<TextViewerHandle>(null);
  const markdownHost = useRef<HTMLDivElement>(null);
  useEffect(() => {
    setSelection(null);
  }, [info.version, encoding, offset, tail, before]);
  // 带行号打开（`ReadingPosition.reveal`）：整份加载的文件在源码里选中并居中；分段读取的大文件没有
  // 行号到字节偏移的换算，只停在开头并说明。切到源码视图由 `FilePreview` 负责。
  const reveal = position?.reveal;
  const wholeFile = page.data?.offset === 0 && page.data.next_offset === null;
  const [revealSkipped, setRevealSkipped] = useState(false);
  useEffect(() => {
    if (reveal && page.data && !wholeFile) {
      setRevealSkipped(true);
      setPosition({ reveal: undefined });
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
  const source = page.data && (
    <CodePreview
      ref={editor}
      key={`${info.path}:${page.data.offset}`}
      text={page.data.text}
      name={info.name}
      onSelection={(value) => {
        // 分段读取的大文件没有「整篇行号」映射：拿到整文件指针，不编造行范围。
        setSelection(
          value
            ? workspaceFileRef(
                info.path,
                page.data.offset === 0 && value.startLine !== undefined && value.endLine !== undefined
                  ? { start: value.startLine, end: value.endLine }
                  : undefined,
              )
            : null,
        );
      }}
      wrap={wrap}
      reveal={wholeFile ? reveal : undefined}
      onRevealed={() => setPosition({ reveal: undefined })}
      scrollTop={position?.scrollTop ?? 0}
      onScroll={(scrollTop) =>
        setPosition({ scrollTop })
      }
    />
  );
  const markdown = page.data && (
    <MarkdownPreview
      hostRef={markdownHost}
      text={info.kind === "mermaid" ? mermaidDocument(page.data.text) : page.data.text}
      onSelectSelection={(value: DomTextSelection | null) => {
        // 渲染视图按块级 data-source-line 求源码行范围；求不出时退化为整文件指针。
        const host = markdownHost.current;
        // Mermaid 的渲染结果是一张图，没有与源码行对应的块：引用整个文件。
        const lines = value && host && info.kind === "markdown" ? sourceLinesOfRange(value.range, host) : null;
        setSelection(value ? workspaceFileRef(info.path, lines ?? undefined) : null);
      }}
      workspaceId={workspaceId}
      path={info.path}
      scrollTop={position?.previewScrollTop ?? 0}
      onScroll={(previewScrollTop) =>
        setPosition({ previewScrollTop })
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
              onSelect={({ line }) => revealSourceLine(line, rendered, markdownHost.current, editor.current)}
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
          />
        </>}
        options={<>
          <EncodingSelect value={encoding} onChange={setEncoding} />
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
            capsule={selection}
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
              setPosition({
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
          <Button size="sm" variant="ghost" onClick={() => setPosition({ tail: true, before: undefined })}>
            末尾
          </Button>
        </div>
      )}
    </div>
  );
}
