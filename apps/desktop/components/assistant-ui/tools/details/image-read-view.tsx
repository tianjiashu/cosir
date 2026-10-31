import { useMemo } from "react";
import { useImageGallery } from "@/components/image/image-gallery";
import { useAttachmentWorkspaceId } from "@/components/assistant-ui/elements/attachment-context";
import { ImageThumbnail } from "@/components/image/image-thumbnail";
import { attachmentContentUrl } from "@/lib/assistant/attachments/image-src";
import { readImageReadDisplay, type ImageReadDisplay } from "../image-read-display";
import type { DetailView } from "./detail-view";

/** 读到的图片并排成方形缩略图，路径放进悬停提示；点开后在这一组里左右翻看。地址按消息附件同一规则解析。 */
function ImageReadThumbnails({ images }: { images: ImageReadDisplay["images"] }) {
  const workspaceId = useAttachmentWorkspaceId();
  const pictures = useMemo(
    () => images.map((image) => ({ src: attachmentContentUrl(image.assetId, workspaceId), alt: image.path })),
    [images, workspaceId],
  );
  const gallery = useImageGallery(pictures);
  return (
    <>
      <ul className="flex flex-wrap gap-2" aria-label="读取的图片">
        {pictures.map((image, index) => (
          <li key={image.alt}>
            <ImageThumbnail src={image.src} name={image.alt} onOpen={() => gallery.open(index)} />
          </li>
        ))}
      </ul>
      {gallery.lightbox}
    </>
  );
}

/**
 * `image-read`（`read_image`）：读到的图片缩略图并排（点开放大），没读成的路径逐条带短提示。
 *
 * 一张不过不整批失败是后端语义（见 `read_image.py`），这里只如实展示两组结果；全部失败时后端返回错误观察，
 * 不带 `image-read`，由外壳显示错误提示。
 */
export const imageReadView: DetailView = {
  accepts: (data) => readImageReadDisplay(data) !== null,
  summary: (data) => {
    const { images, failures } = readImageReadDisplay(data)!;
    return failures.length ? `${images.length} 张，${failures.length} 张未读取` : `${images.length} 张`;
  },
  body: (data) => {
    const { images, failures } = readImageReadDisplay(data)!;
    return (
      <div className="space-y-2">
        {images.length > 0 && <ImageReadThumbnails images={images} />}
        {failures.length > 0 && (
          <ul className="text-muted-foreground flex flex-col gap-0.5 text-xs" aria-label="未读取的图片">
            {failures.map((failure) => (
              <li key={failure.path} className="flex min-w-0 gap-2">
                <span className="min-w-0 truncate" title={failure.path}>{failure.path}</span>
                <span className="text-warning shrink-0">{failure.hint}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    );
  },
};
