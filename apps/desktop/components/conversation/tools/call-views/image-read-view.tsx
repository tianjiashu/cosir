import { useMemo } from "react";
import { useAttachmentWorkspaceId } from "@/components/conversation/thread/attachment-context";
import { ImageStrip } from "@/components/image/image-strip";
import { attachmentContentUrl } from "@/lib/assistant/attachments/image-src";
import { readImageReadDisplay, type ImageReadDisplay } from "../image-read-display";
import type { CallView } from "../call-view";

/** 读到的图片按对话图片的统一外观横排，路径放进悬停提示；地址按消息附件同一规则解析。 */
function ImageReadThumbnails({ images }: { images: ImageReadDisplay["images"]; }) {
  const workspaceId = useAttachmentWorkspaceId();
  const pictures = useMemo(
    () => images.map((image) => ({ src: attachmentContentUrl(image.assetId, workspaceId), alt: image.path, title: image.path })),
    [images, workspaceId],
  );
  return <ImageStrip images={pictures} label="读取的图片" />;
}

/**
 * `image-read`（`read_image`）：读到的图片缩略图并排（点开放大），没读成的路径逐条带短提示。
 *
 * 一张不过不整批失败是后端语义（见 `read_image.py`），这里只如实展示两组结果；全部失败时后端返回错误观察，
 * 不带 `image-read`，由外壳显示错误提示。
 */
export const imageReadView: CallView = (data) => {
  const display = readImageReadDisplay(data);
  if (!display) return null;
  const { images, failures } = display;
  return {
    meta: images.length > 0 ? `${images.length} 张` : undefined, detail: {
      render: () => (
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
      ),
    }
  };
};
