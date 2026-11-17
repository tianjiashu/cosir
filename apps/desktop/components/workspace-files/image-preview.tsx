import { useEffect, useState, type ReactNode } from "react";
import { ZoomInIcon, ZoomOutIcon } from "lucide-react";
import type { FileInfo } from "@/lib/api/workspace-files";
import { useFileResourceSrc, useResourceLoadFailure } from "@/lib/workspace-files/resources";
import { LoadingState } from "@/components/ui/state-panel";
import { Button } from "@/components/ui/button";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { AddToChatButton } from "./add-to-chat-button";
import { ReadingToolbar, type ReadingNavigation } from "./reading-toolbar";

/**
 * 位图与 SVG 的渲染视图：缩放控件放进共用工具栏，尺寸与缩略说明放进「更多」。
 *
 * 资源接口对大图返回缩小图，所以「尺寸」与「100%」都按文件信息里的原图宽高（位图）算，不按 `<img>` 拿到的
 * 画面；SVG 没有原图宽高，退回画面尺寸。位图宽高为空（后端读不出）的情形由 `FilePreview` 在挂载前就转为
 * 「看不了的文件」；这里只在加载失败且重读后版本没变时经 `onUnviewable` 上报。
 */
export function ImagePreview({
  workspaceId,
  info,
  navigation,
  view,
  onUnviewable,
}: {
  workspaceId: number;
  info: FileInfo;
  navigation: ReadingNavigation;
  view?: ReactNode;
  onUnviewable: (reason: string) => void;
}) {
  const src = useFileResourceSrc(workspaceId, info.path, info.version);
  const failure = useResourceLoadFailure(workspaceId, info.path, src);
  useEffect(() => {
    if (failure.failed) onUnviewable("这张图片的内容无法显示");
  }, [failure.failed, onUnviewable]);
  const [zoom, setZoom] = useState<number | null>(null);
  const [natural, setNatural] = useState({ width: 0, height: 0 });
  const size = info.width !== null && info.height !== null ? { width: info.width, height: info.height } : natural;
  const reduced = info.width !== null && natural.width > 0 && natural.width < info.width;
  return (
    <div className="flex h-full min-h-0 flex-col">
      <ReadingToolbar navigation={navigation} info={info} view={view}
        actions={<>
          <TooltipIconButton tooltip="缩小" aria-label="缩小" className="size-8 shrink-0"
            onClick={() => setZoom(Math.max(0.1, (zoom ?? 1) / 1.25))}>
            <ZoomOutIcon />
          </TooltipIconButton>
          <Button size="sm" variant="ghost" className="min-w-12 px-1 tabular-nums"
            title={zoom === null ? "按原始尺寸显示" : "适应面板"}
            onClick={() => setZoom(zoom === null ? 1 : null)}>
            {zoom === null ? "适应" : `${Math.round(zoom * 100)}%`}
          </Button>
          <TooltipIconButton tooltip="放大" aria-label="放大" className="size-8 shrink-0"
            onClick={() => setZoom(Math.min(8, (zoom ?? 1) * 1.25))}>
            <ZoomInIcon />
          </TooltipIconButton>
          <AddToChatButton workspaceId={workspaceId} path={info.path} />
        </>}
        options={<p className="text-xs text-muted-foreground">
          {size.width > 0 ? `${size.width} × ${size.height}` : "尺寸读取中"}
          {reduced && " · 大图缩略预览"}
        </p>}
      />
      <div className="flex min-h-0 flex-1 overflow-auto p-4">
        {src ? (
          <img
            src={src}
            alt={info.name}
            onError={() => void failure.onError()}
            onLoad={(event) =>
              setNatural({
                width: event.currentTarget.naturalWidth,
                height: event.currentTarget.naturalHeight,
              })
            }
            style={
              zoom === null
                ? {
                    maxWidth: "100%",
                    maxHeight: "100%",
                    objectFit: "contain",
                    margin: "auto",
                  }
                : {
                    width: size.width * zoom || undefined,
                    maxWidth: "none",
                    height: "auto",
                    alignSelf: "flex-start",
                  }
            }
          />
        ) : (
          <LoadingState className="m-auto" label="正在读取图片…" />
        )}
      </div>
    </div>
  );
}
