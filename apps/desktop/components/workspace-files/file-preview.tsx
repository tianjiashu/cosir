import { AddToChatButton } from "./add-to-chat-button";
import { ReadingToolbar, type ReadingNavigation } from "./reading-toolbar";
import { ViewToggle } from "@/components/ui/view-toggle";
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { LoadingState } from "@/components/ui/state-panel";
import { FileReadProblem } from "./file-read-problem";
import { Button } from "@/components/ui/button";
import { TextPreview } from "./text-preview";
import { ImagePreview } from "./image-preview";
import { fileInfoQueryOptions } from "@/lib/query/query-options";
import { fileSession, useFilesStore, type ReadingView } from "@/lib/workspace-files/store";
import type { FileInfo } from "@/lib/api/workspace-files";
const PdfPreview = lazy(() => import("./pdf-preview"));

/** 既有渲染形态又能看源码的类型；纯文本只有源码，位图与 PDF 只有渲染结果。 */
const SWITCHABLE: ReadonlySet<FileInfo["kind"]> = new Set(["markdown", "svg", "binary"]);

/**
 * 按文件类型选择预览器，并持有「渲染 / 源码」这一个视图事实（存在工作区会话的阅读偏好里，切换标签不丢）。
 * 视图切换按钮在这里生成、交给预览器放进自己的工具栏；工具栏本身由各预览器渲染。
 */
export function FilePreview({
  workspaceId,
  path,
  navigation,
}: {
  workspaceId: number;
  path: string;
  navigation: ReadingNavigation;
}) {
  const query = useQuery({
    ...fileInfoQueryOptions(workspaceId, path),
  });
  const preference = useFilesStore((s) => fileSession(s.sessions, workspaceId).preferences[path]);
  const reveal = useFilesStore((s) => fileSession(s.sessions, workspaceId).positions[path]?.reveal);
  const setPreference = useFilesStore((s) => s.preference);
  const setView = (view: ReadingView) => setPreference(workspaceId, path, { view });
  const kind = query.data?.kind;
  const view: ReadingView = kind === "text" ? "source" : (preference?.view ?? "rendered");
  // 带行号打开：渲染视图没有行的概念，先切到源码，再由文本预览选中并居中。
  useEffect(() => {
    if (reveal && kind && SWITCHABLE.has(kind) && view === "rendered")
      setPreference(workspaceId, path, { view: "source" });
  }, [reveal, kind, view, workspaceId, path, setPreference]);
  const [updated, setUpdated] = useState(false);
  const previous = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (previous.current && previous.current !== query.data?.version)
      setUpdated(true);
    previous.current = query.data?.version;
  }, [query.data?.version]);
  if (query.error)
    return (
      <>
        <ReadingToolbar navigation={navigation} />
        <FileReadProblem workspaceId={workspaceId} path={path} error={query.error} onRetry={() => void query.refetch()} />
      </>
    );
  if (!query.data) return <><ReadingToolbar navigation={navigation} /><LoadingState label="正在读取文件信息…" /></>;
  const info = query.data;
  const toggle = SWITCHABLE.has(info.kind) ? <ViewToggle rendered={view === "rendered"} onToggle={() => setView(view === "rendered" ? "source" : "rendered")} /> : undefined;
  let content;
  if (view === "source" || info.kind === "markdown")
    content = (
      <TextPreview workspaceId={workspaceId} info={info} navigation={navigation}
        rendered={info.kind === "markdown" && view === "rendered"} view={toggle} />
    );
  else if (info.kind === "image" || info.kind === "svg")
    content = (
      <ImagePreview key={info.version} workspaceId={workspaceId} info={info} navigation={navigation} view={toggle} />
    );
  else if (info.kind === "pdf")
    content = (
      <Suspense fallback={<><ReadingToolbar navigation={navigation} /><LoadingState label="正在加载 PDF 阅读器…" /></>}>
        <PdfPreview workspaceId={workspaceId} info={info} navigation={navigation} />
      </Suspense>
    );
  else
    content = (
      <>
        <ReadingToolbar navigation={navigation} info={info} view={toggle}
          actions={<AddToChatButton workspaceId={workspaceId} path={path} />} />
        <div className="space-y-3 p-6 text-sm">
          <p>此类型无内置内容预览</p>
          <p className="text-muted-foreground">
            {info.mime} · {info.size.toLocaleString()} 字节
          </p>
          <Button variant="outline" onClick={() => setView("source")}>
            以文本方式查看
          </Button>
        </div>
      </>
    );
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex min-h-0 flex-1 flex-col">{content}</div>
      {updated && <div role="status" className="shrink-0 border-t px-3 py-1 text-xs text-muted-foreground">文件已更新</div>}
    </div>
  );
}
