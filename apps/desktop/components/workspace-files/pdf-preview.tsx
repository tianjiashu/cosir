import {
  getBackendRuntimeSnapshot,
  subscribeBackendRuntime,
} from "@/src/runtime-config";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import * as pdfjs from "pdfjs-dist";
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

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

/** PDF.js viewer 负责页面回收、文本层与查找；销毁 loadingTask 同时终止 range 请求及 worker。 */
export default function PdfPreview({
  workspaceId,
  info,
}: {
  workspaceId: number;
  info: FileInfo;
}) {
  const backend = useSyncExternalStore(
    subscribeBackendRuntime,
    getBackendRuntimeSnapshot,
    getBackendRuntimeSnapshot,
  );
  const container = useRef<HTMLDivElement>(null);
  const viewerElement = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<PDFViewer | null>(null);
  const busRef = useRef<EventBus | null>(null);
  const [page, setPage] = useState(1);
  const [pages, setPages] = useState(0);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
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
  const find = (previous = false) =>
    busRef.current?.dispatch("find", {
      type: "again",
      query,
      caseSensitive: false,
      entireWord: false,
      highlightAll: true,
      findPrevious: previous,
    });
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b p-2 text-xs">
        <Button
          size="sm"
          variant="ghost"
          disabled={page <= 1}
          onClick={() => viewerRef.current?.previousPage()}
        >
          上一页
        </Button>
        <span>
          {page} / {pages || "…"}
        </span>
        <Button
          size="sm"
          variant="ghost"
          disabled={page >= pages}
          onClick={() => viewerRef.current?.nextPage()}
        >
          下一页
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => viewerRef.current?.decreaseScale()}
        >
          −
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => viewerRef.current?.increaseScale()}
        >
          ＋
        </Button>
        <input
          aria-label="查找 PDF 内容"
          className="w-28 rounded border bg-background px-2 py-1"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") find(e.shiftKey);
          }}
        />
        <Button size="sm" variant="ghost" onClick={() => find()}>
          查找
        </Button>
      </div>
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
          <input
            type="password"
            aria-label="PDF 密码"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="rounded border px-2"
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
        <div ref={container} className="absolute inset-0 overflow-auto">
          <div ref={viewerElement} className="pdfViewer" />
        </div>
      </div>
    </div>
  );
}
