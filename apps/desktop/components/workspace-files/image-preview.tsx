import { useState, type ReactNode } from "react";
import { ZoomInIcon, ZoomOutIcon } from "lucide-react";
import type { FileInfo } from "@/lib/api/workspace-files";
import { useImageResource } from "@/lib/workspace-files/resources";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import { Button } from "@/components/ui/button";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { AddToChatButton } from "./add-to-chat-button";
import { ReadingToolbar, type ReadingNavigation } from "./reading-toolbar";

/** 位图与 SVG 的渲染视图：缩放控件放进共用工具栏，尺寸与缩略说明放进「更多」。 */
export function ImagePreview({
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
  const resource = useImageResource(workspaceId, info.path, info.version);
  const [zoom, setZoom] = useState<number | null>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [error, setError] = useState(false);
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
          {resource.thumbnail && " · 大图缩略预览"}
        </p>}
      />
      <div className="flex min-h-0 flex-1 overflow-auto p-4">
        {resource.error || error ? (
          <ErrorState className="m-auto" message={resource.error ?? "此图片无法解码"} />
        ) : resource.url ? (
          <img
            src={resource.url}
            alt={info.name}
            onError={() => setError(true)}
            onLoad={(event) =>
              setSize({
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
