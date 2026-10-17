import { SelectionActions } from "@/components/workspace-references/selection-actions";
import { ReadingToolbar, type ReadingNavigation } from "./reading-toolbar";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getFileInfo } from "@/lib/api/workspace-files";
import { queryKeys } from "@/lib/query/query-keys";
import { Button } from "@/components/ui/button";
import { TextPreview } from "./text-preview";
import { ImagePreview } from "./image-preview";
const PdfPreview = lazy(() => import("./pdf-preview"));

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
    queryKey: queryKeys.fileInfo(workspaceId, path),
    queryFn: ({ signal }) => getFileInfo(workspaceId, path, signal),
  });
  const [asText, setAsText] = useState(false);
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
        <div role="alert" className="p-4 text-sm">
          {userErrorMessage(query.error, "文件读取失败")}
          <Button variant="ghost" onClick={() => void query.refetch()}>
            重新读取
          </Button>
        </div>
      </>
    );
  if (!query.data) return <><ReadingToolbar navigation={navigation} /><p className="p-4 text-sm">正在读取文件信息…</p></>;
  const info = query.data;
  const view = info.kind === "svg" ? (
    <Button size="sm" variant="ghost" onClick={() => setAsText(!asText)}>{asText ? "图片" : "源码"}</Button>
  ) : undefined;
  const text = asText || ["text", "markdown"].includes(info.kind);
  let content;
  if (text)
    content = <TextPreview workspaceId={workspaceId} info={info} navigation={navigation} view={view} />;
  else if (info.kind === "image" || info.kind === "svg")
    content = (
      <ImagePreview key={info.version} workspaceId={workspaceId} info={info} />
    );
  else if (info.kind === "pdf")
    content = (
      <Suspense fallback={<p className="p-4">正在加载 PDF 阅读器…</p>}>
        <PdfPreview workspaceId={workspaceId} info={info} />
      </Suspense>
    );
  else
    content = (
      <div className="space-y-3 p-6 text-sm">
        <p>此类型无内置内容预览</p>
        <p className="text-muted-foreground">
          {info.mime} · {info.size.toLocaleString()} 字节
        </p>
        <Button variant="outline" onClick={() => setAsText(true)}>
          以文本方式查看
        </Button>
      </div>
    );
  return (
    <div className="flex h-full min-h-0 flex-col">
      {!text && <ReadingToolbar navigation={navigation} info={info} view={view}
        actions={<SelectionActions workspaceId={workspaceId} path={path} />} />}
      <div className="min-h-0 flex-1">{content}</div>
      {updated && <div role="status" className="shrink-0 border-t px-3 py-1 text-xs text-muted-foreground">文件已更新</div>}
    </div>
  );
}
