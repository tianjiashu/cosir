import { userErrorMessage } from "@/lib/errors/user-error-message";
import { useEffect, useRef, useState } from "react";
import * as pdfjs from "pdfjs-dist";
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  SearchIcon,
  ZoomInIcon,
  ZoomOutIcon,
} from "lucide-react";
import {
  EventBus,
  PDFViewer,
  PDFLinkService,
  PDFFindController,
} from "pdfjs-dist/web/pdf_viewer.mjs";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import "pdfjs-dist/web/pdf_viewer.css";
import type { FileInfo } from "@/lib/api/workspace-files";
import { pdfResource } from "@/lib/workspace-files/resources";
import { ErrorState } from "@/components/ui/state-panel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { ReferenceSelectionMenu } from "@/components/workspace-references/selection-actions";
import { pdfPageOfRange } from "@/lib/workspace-files/dom-selection-locator";
import { workspaceFileRef, workspacePageRef } from "@/lib/workspace-references/ref-capsule";
import type { RefCapsule } from "@/lib/assistant/capsules";
import { workspaceFiles, useFilesStore } from "@/lib/workspace-files/workspace-files-store";
import { useDomSelection } from "@/hooks/use-dom-selection";
import { AddToChatButton } from "./add-to-chat-button";
import { useBackendRuntime } from "@/lib/backend/use-backend-runtime";
import { useReadingCommand } from "@/lib/workspace-files/reading-commands";
import { ReadingToolbar, type ReadingNavigation } from "./reading-toolbar";
import { FindBar } from "@/components/ui/find-bar";
import type { FindDirection, FindResult } from "@/lib/find/next-match";

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

/**
 * PDF.js viewer 负责页面回收、文本层与查找；销毁 loadingSession 同时终止 range 请求及 worker。
 * 翻页与查找入口放进共用工具栏；查找沿用 PDF.js 的查找控制器（逐页文本层高亮），查找条是应用共用的
 * `FindBar`，命中位置来自控制器的 `updatefindmatchescount` 事件。
 */
export default function PdfPreview({
  workspaceId,
  info,
  navigation,
}: {
  workspaceId: number;
  info: FileInfo;
  navigation: ReadingNavigation;
}) {
  const backend = useBackendRuntime();
  const container = useRef<HTMLDivElement>(null);
  const viewerElement = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<PDFViewer | null>(null);
  const busRef = useRef<EventBus | null>(null);
  const [page, setPage] = useState(1);
  const [pages, setPages] = useState(0);
  const [error, setError] = useState("");
  // 文本层里选中的文字作为「当前页」引用添加到对话（与 Markdown 渲染视图同一规则）。
  const [selection, setSelection] = useState<RefCapsule | null>(null);
  useDomSelection(container, (value) => {
    if (!value || !container.current) {
      setSelection(null);
      return;
    }
    const pageNumber = pdfPageOfRange(value.range, container.current);
    setSelection(pageNumber !== null ? workspacePageRef(info.path, pageNumber) : workspaceFileRef(info.path));
  });
  // 引用带页码打开时跳到该页；跳完清除，之后照常按滚动位置恢复。
  const revealPage = useFilesStore((s) => workspaceFiles(s.byWorkspace, workspaceId).positions[info.path]?.revealPage);
  useEffect(() => {
    if (revealPage === undefined) return;
    const viewer = viewerRef.current;
    if (!viewer || !pages) return;
    viewer.currentPageNumber = Math.min(Math.max(revealPage, 1), pages);
    useFilesStore.getState().position(workspaceId, info.path, { revealPage: undefined });
  }, [revealPage, pages, workspaceId, info.path]);
  useEffect(() => setSelection(null), [info.path, info.version]);
  const [showSearch, setShowSearch] = useState(false);
  const [focusKey, setFocusKey] = useState(0);
  const [findResult, setFindResult] = useState<FindResult | null>(null);
  const lastQuery = useRef("");
  const searchTrigger = useRef<HTMLButtonElement>(null);
  useReadingCommand("find", () => {
    setShowSearch(true);
    setFocusKey((key) => key + 1);
  });
  const closeSearch = () => {
    busRef.current?.dispatch("findbarclose", { source: null });
    lastQuery.current = "";
    setFindResult(null);
    setShowSearch(false);
    searchTrigger.current?.focus();
  };
  const [password, setPassword] = useState("");
  const [passwordRequired, setPasswordRequired] = useState(false);
  const passwordCallback = useRef<((password: string) => void) | null>(null);
  useEffect(() => {
    if (!container.current || !viewerElement.current) return;
    let disposed = false;
    setError("");
    setPages(0);
    setPage(1);
    setPasswordRequired(false);
    setPassword("");
    passwordCallback.current = null;
    const eventBus = new EventBus();
    const linkService = new PDFLinkService({ eventBus });
    const findController = new PDFFindController({ eventBus, linkService });
    const viewer = new PDFViewer({
      container: container.current,
      viewer: viewerElement.current,
      eventBus,
      linkService,
      findController,
    });
    viewerRef.current = viewer;
    busRef.current = eventBus;
    linkService.setViewer(viewer);
    eventBus.on("pagesinit", () => {
      viewer.currentScaleValue = "page-width";
    });
    eventBus.on("pagechanging", (event: { pageNumber: number }) =>
      setPage(event.pageNumber),
    );
    const onMatches = ({ matchesCount }: { matchesCount: { current: number; total: number } }) =>
      setFindResult({ index: matchesCount.current - 1, count: matchesCount.total });
    eventBus.on("updatefindmatchescount", onMatches);
    eventBus.on("updatefindcontrolstate", onMatches);
    const assets = `${import.meta.env.BASE_URL}pdf-assets/`;
    const loading = pdfjs.getDocument({
      ...pdfResource(workspaceId, info),
      cMapUrl: `${assets}cmaps/`,
      cMapPacked: true,
      standardFontDataUrl: `${assets}standard_fonts/`,
      wasmUrl: `${assets}wasm/`,
      disableAutoFetch: true,
      disableStream: true,
    });
    loading.onPassword = (callback: (password: string) => void) => {
      passwordCallback.current = callback;
      setPasswordRequired(true);
    };
    void loading.promise
      .then((document) => {
        if (disposed) return;
        viewer.setDocument(document);
        linkService.setDocument(document);
        setPages(document.numPages);
      })
      .catch((cause) => {
        if (!disposed) setError(userErrorMessage(cause, "PDF 无法读取"));
      });
    const resize = new ResizeObserver(() => {
      if (viewer.pagesCount) viewer.currentScaleValue = "page-width";
    });
    resize.observe(container.current);
    return () => {
      disposed = true;
      passwordCallback.current = null;
      resize.disconnect();
      viewer.setDocument(null);
      linkService.setDocument(null);
      void loading.destroy();
      viewerRef.current = null;
      busRef.current = null;
    };
  }, [workspaceId, info.path, info.version, backend]);
  // 换了词是一次新查找（type ""），同一个词是「再找一处」（type "again"）。
  const find = (query: string, direction: FindDirection) => {
    const again = query === lastQuery.current;
    lastQuery.current = query;
    if (!query) setFindResult(null);
    busRef.current?.dispatch("find", {
      type: again ? "again" : "",
      query,
      caseSensitive: false,
      entireWord: false,
      highlightAll: true,
      findPrevious: direction === "previous",
    });
  };
  return (
    <div className="flex h-full min-h-0 flex-col">
      <ReadingToolbar navigation={navigation} info={info}
        actions={<>
          <TooltipIconButton tooltip="上一页" aria-label="上一页" className="size-8 shrink-0"
            disabled={page <= 1} onClick={() => viewerRef.current?.previousPage()}>
            <ChevronLeftIcon />
          </TooltipIconButton>
          <span className="tabular-nums">{page} / {pages || "…"}</span>
          <TooltipIconButton tooltip="下一页" aria-label="下一页" className="size-8 shrink-0"
            disabled={page >= pages} onClick={() => viewerRef.current?.nextPage()}>
            <ChevronRightIcon />
          </TooltipIconButton>
          <TooltipIconButton ref={searchTrigger} tooltip="查找 PDF 内容" aria-label="查找 PDF 内容" className="size-8 shrink-0"
            aria-expanded={showSearch} onClick={() => showSearch ? closeSearch() : setShowSearch(true)}>
            <SearchIcon />
          </TooltipIconButton>
          <AddToChatButton workspaceId={workspaceId} path={info.path} selection={selection} />
        </>}
        // 页面默认随面板宽度适配，缩放是少用操作，放进「更多」让窄面板的工具栏保持一行。
        options={<div className="flex gap-1">
          <Button size="sm" variant="ghost" onClick={() => viewerRef.current?.decreaseScale()}><ZoomOutIcon />缩小</Button>
          <Button size="sm" variant="ghost" onClick={() => viewerRef.current?.increaseScale()}><ZoomInIcon />放大</Button>
        </div>}
      />
      {passwordRequired && (
        <form
          className="flex gap-2 p-3"
          onSubmit={(event) => {
            event.preventDefault();
            passwordCallback.current?.(password);
            setPasswordRequired(false);
            setPassword("");
          }}
        >
          <Input
            type="password"
            aria-label="PDF 密码"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="h-8 max-w-60"
          />
          <Button size="sm" type="submit">
            打开 PDF
          </Button>
        </form>
      )}
      {error && (
        <ErrorState className="m-3" message={error} />
      )}
      <div className="relative min-h-0 flex-1">
        {showSearch && (
          <FindBar label="在 PDF 中查找" className="absolute top-2 right-4 z-10" result={findResult}
            onSearch={find} onClose={closeSearch} focusKey={focusKey} />
        )}
        <ReferenceSelectionMenu workspaceId={workspaceId} capsule={selection}>
          <div ref={container} className="absolute inset-0 overflow-auto">
            <div ref={viewerElement} className="pdfViewer" />
          </div>
        </ReferenceSelectionMenu>
      </div>
    </div>
  );
}
