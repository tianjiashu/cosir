import { useQueryClient } from "@tanstack/react-query";
import { ImageOffIcon } from "lucide-react";

import { useImageGallery } from "@/components/image/image-gallery";
import { ImageStrip, type StripImage } from "@/components/image/image-strip";
import { useReferenceBridge } from "@/components/workspace-references/reference-bridge";
import { isUnreadableImage } from "@/lib/api/workspace-files";
import type { TransportDeliverable } from "@/lib/assistant/contract";
import type { DeliverableEntry, DeliverableImageCard as DeliverableImageCardData } from "@/lib/assistant/turn/deliverable-layout";
import { useBackendRuntime } from "@/lib/backend/use-backend-runtime";
import { baseName } from "@/lib/files/file-name";
import { cn } from "@/lib/utils";
import { confirmResourceFailure, fileResourceUrl, useResourceLoadFailure } from "@/lib/workspace-files/resources";
import { useDeliverableFile, useDeliverableFiles, type DeliverableFileState } from "./use-deliverable-file";

type FileDeliverable = Extract<TransportDeliverable, { type: "file" }>;

/** 方块上不写文件名，提示条给出说明与路径；图已经不在时给出原因。 */
function stripImage(
  deliverable: FileDeliverable,
  state: DeliverableFileState,
  workspaceId: number | null,
  onStale: (path: string, src: string) => void,
): StripImage {
  const path = deliverable.workspacePath!;
  const version = state.info?.version;
  const src = version !== undefined && workspaceId !== null ? fileResourceUrl(workspaceId, path, version) : undefined;
  // SVG 不经 Pillow：方块与灯箱都用原始地址。
  const previewSrc = src && deliverable.kind === "image" ? fileResourceUrl(workspaceId!, path, version!, "tile") : undefined;
  const unreadable = state.info !== null && isUnreadableImage(state.info);
  const unavailable = state.problem === "gone" ? "已不存在" : state.problem === "denied" ? "无法访问" : unreadable ? "无法预览" : undefined;
  return {
    src,
    previewSrc,
    alt: baseName(path),
    title: deliverable.caption ? `${deliverable.caption}\n${path}` : path,
    unavailable: unavailable && `${baseName(path)} · ${unavailable}`,
    onError: () => onStale(path, previewSrc ?? src!),
  };
}

/** 单张大图卡上图片显示不了的原因；能显示时为空。 */
function cardProblem(state: DeliverableFileState, failed: boolean): string | null {
  if (state.problem === "gone") return "已不存在";
  if (state.problem === "denied") return "无法访问";
  if (failed || (state.info !== null && isUnreadableImage(state.info))) return "无法预览";
  return null;
}

/**
 * 单张大图卡：按原图比例完整显示、不裁切，点开进灯箱看原图。尺寸与是否成卡由布局按交付记录定好（`deliverableGroups`），
 * 这里不再判定：文件信息还没读到时先占好同样大小的框；图被删、无法访问或加载失败时在同一个框里说明原因，高度不跳。
 */
export function DeliverableImageCard({ card }: { card: DeliverableImageCardData }) {
  useBackendRuntime();
  const deliverable = card.entry.deliverable as FileDeliverable;
  const path = deliverable.workspacePath!;
  const workspaceId = useReferenceBridge()?.workspaceId ?? null;
  const state = useDeliverableFile(deliverable);
  const version = state.info?.version;
  const src = version !== undefined && workspaceId !== null ? fileResourceUrl(workspaceId, path, version) : undefined;
  const cardSrc = version !== undefined && workspaceId !== null ? fileResourceUrl(workspaceId, path, version, "card") : undefined;
  const failure = useResourceLoadFailure(workspaceId ?? 0, path, cardSrc);
  const problem = cardProblem(state, failure.failed);
  const image: StripImage = { src, alt: baseName(path), title: deliverable.caption ? `${deliverable.caption}\n${path}` : path };
  const gallery = useImageGallery([image]);
  const frame = "bg-muted/40 border-border/70 block max-w-full overflow-hidden rounded-lg border";
  if (problem || !src)
    return (
      <div data-slot="deliverable-image-card" data-unavailable={problem ? "true" : undefined} title={problem ? `${image.alt} · ${problem}` : image.title} style={card.size} className={cn(frame, "relative")}>
        {problem && (
          <span className="text-muted-foreground absolute inset-0 m-auto flex flex-col items-center justify-center gap-1 text-xs">
            <ImageOffIcon className="size-6" aria-hidden />
            {problem}
          </span>
        )}
      </div>
    );
  return (
    <>
      <button
        type="button"
        data-slot="deliverable-image-card"
        aria-label={`预览图片 ${image.alt}`}
        title={image.title}
        onClick={() => gallery.open(0)}
        style={card.size}
        className={cn(frame, "focus-visible:ring-ring cursor-zoom-in outline-none focus-visible:ring-2")}
      >
        <img src={cardSrc} alt={image.alt} decoding="async" onError={failure.onError} className="block size-full object-contain" />
      </button>
      {gallery.lightbox}
    </>
  );
}

/**
 * 交付物里的一组图片（工作区内的位图与 SVG）：复用 `ImageStrip`，方块用 `preview=tile` 小图、灯箱用原图。资源地址带
 * 现读的版本；加载失败时经 `confirmResourceFailure` 判定（版本过期就换新地址），破图标由方块按地址自己显示。
 * 图被删或无法访问时保持方块、换中性图标，高度不变。只有一张且能成卡时由布局交给 `DeliverableImageCard`。
 */
export function DeliverableTileRow({ entries }: { entries: readonly DeliverableEntry[] }) {
  useBackendRuntime();
  const client = useQueryClient();
  const deliverables = entries.map((entry) => entry.deliverable as FileDeliverable);
  const { workspaceId, states } = useDeliverableFiles(deliverables);
  const onStale = (path: string, src: string) => {
    if (workspaceId !== null) void confirmResourceFailure(client, workspaceId, path, src);
  };
  const images = deliverables.map((deliverable, index) => stripImage(deliverable, states[index], workspaceId, onStale));
  return <ImageStrip images={images} label="交付的图片" />;
}
