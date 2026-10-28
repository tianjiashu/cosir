import { useState } from "react";
import type { FileInfo } from "@/lib/api/workspace-files";
import { useImageResource } from "@/lib/workspace-files/resources";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import { Button } from "@/components/ui/button";

export function ImagePreview({
  workspaceId,
  info,
}: {
  workspaceId: number;
  info: FileInfo;
}) {
  const resource = useImageResource(workspaceId, info.path, info.version);
  const [zoom, setZoom] = useState<number | null>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [error, setError] = useState(false);
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b px-3 py-1 text-xs">
        {resource.thumbnail && <span>大图缩略预览</span>}
        <Button size="sm" variant="ghost" onClick={() => setZoom(null)}>
          适应面板
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setZoom(1)}>
          100%
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => setZoom(Math.max(0.1, (zoom ?? 1) / 1.25))}
        >
          −
        </Button>
        <span>{zoom === null ? "适应" : `${Math.round(zoom * 100)}%`}</span>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => setZoom(Math.min(8, (zoom ?? 1) * 1.25))}
        >
          ＋
        </Button>
        {size.width > 0 && (
          <span>
            {size.width} × {size.height}
          </span>
        )}
      </div>
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
