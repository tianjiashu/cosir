import { ReadingToolbar, type ReadingNavigation } from "./reading-toolbar";
import { ViewToggle } from "@/components/ui/view-toggle";
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { LoadingState } from "@/components/ui/state-panel";
import { FileReadProblem } from "./file-read-problem";
import { TextPreview } from "./text-preview";
import { TableReader } from "./table-reader";
import { ImagePreview } from "./image-preview";
import { isUnreadableImage } from "@/lib/api/workspace-files";
import { fileInfoQueryOptions } from "@/lib/query/query-options";
import { workspaceFiles, useFilesStore, type ReadingView } from "@/lib/workspace-files/workspace-files-store";
import { filePreviewMode, opensLinesInSource } from "@/lib/workspace-files/file-preview-mode";
import { frontendLog } from "@/lib/logging/frontend-log";
import { BinaryPreview } from "./binary-preview";
import { MediaPreview } from "./media-preview";
const PdfPreview = lazy(() => import("./pdf-preview"));

/**
 * 文件面板的预览：持有「显示不了」这个当前版本的瞬时事实与视图偏好，用哪个预览器、要不要切换按钮交给
 * `filePreviewMode`（规则只在那里），这里只按结果挂组件。预览器显示不了（格式不支持、内容损坏）时经 `onUnviewable`
 * 上报原因，统一切到「看不了的文件」，不让各预览器各自停在错误页。视图切换按钮在这里生成、交给预览器放进自己的
 * 工具栏；工具栏本身由各预览器渲染。
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
  const preference = useFilesStore((s) => workspaceFiles(s.byWorkspace, workspaceId).preferences[path]);
  const reveal = useFilesStore((s) => workspaceFiles(s.byWorkspace, workspaceId).positions[path]?.reveal);
  const setPreference = useFilesStore((s) => s.preference);
  const setView = (view: ReadingView) => setPreference(workspaceId, path, { view });
  const kind = query.data?.kind;
  const view: ReadingView | undefined = kind && filePreviewMode(kind, preference?.view, null).view;
  // 带行号打开：渲染视图没有行的概念，先切到源码，再由文本预览选中并居中。
  useEffect(() => {
    if (reveal && kind && opensLinesInSource(kind) && view === "rendered")
      setPreference(workspaceId, path, { view: "source" });
  }, [reveal, kind, view, workspaceId, path, setPreference]);
  const [unviewable, setUnviewable] = useState<{ version: string; reason: string } | null>(null);
  // 引用稳定（只随版本变），预览器可以放心放进 effect 依赖：PDF 阅读器不会因父组件重渲染而重载。
  const version = query.data?.version;
  const onUnviewable = useCallback((reason: string) => {
    // 回调只在文件信息读到之后才交给预览器，版本此时一定存在。
    setUnviewable({ version: version!, reason });
    void frontendLog("WARNING", "workspace_file_unviewable", "文件无法在内置预览器中显示", { data: { workspaceId, path, kind, reason } });
  }, [version, kind, workspaceId, path]);
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
  // 「显示不了」只认当前版本：文件变了（版本换新）就重新尝试预览。宽高为空的位图在请求资源前就判定。
  const unviewableReason = unviewable?.version === info.version ? unviewable.reason
    : isUnreadableImage(info) ? "图片已损坏或像素过大，无法预览" : null;
  const mode = filePreviewMode(info.kind, preference?.view, unviewableReason);
  const toggle = mode.switchable
    ? <ViewToggle rendered={mode.view === "rendered"} onToggle={() => setView(mode.view === "rendered" ? "source" : "rendered")} />
    : undefined;
  let content;
  if (mode.surface === "text")
    content = (
      <TextPreview workspaceId={workspaceId} info={info} navigation={navigation} rendered={mode.view === "rendered"} view={toggle} />
    );
  else if (mode.surface === "table")
    content = <TableReader workspaceId={workspaceId} info={info} navigation={navigation} view={toggle} onUnviewable={onUnviewable} />;
  else if (mode.surface === "binary")
    content = (
      <BinaryPreview workspaceId={workspaceId} info={info} navigation={navigation}
        reason={unviewableReason} view={toggle} onViewAsText={() => setView("source")} />
    );
  else if (mode.surface === "image")
    content = (
      <ImagePreview key={info.version} workspaceId={workspaceId} info={info} navigation={navigation} view={toggle} onUnviewable={onUnviewable} />
    );
  else if (mode.surface === "media")
    content = <MediaPreview key={info.version} workspaceId={workspaceId} info={info} navigation={navigation} onUnviewable={onUnviewable} />;
  else
    content = (
      <Suspense fallback={<><ReadingToolbar navigation={navigation} /><LoadingState label="正在加载 PDF 阅读器…" /></>}>
        <PdfPreview workspaceId={workspaceId} info={info} navigation={navigation} onUnviewable={onUnviewable} />
      </Suspense>
    );
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex min-h-0 flex-1 flex-col">{content}</div>
      {updated && <div role="status" className="shrink-0 border-t px-3 py-1 text-xs text-muted-foreground">文件已更新</div>}
    </div>
  );
}
