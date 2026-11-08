import { createContext, useContext, useMemo, type FC, type PropsWithChildren } from "react";
import {
  ComposerPrimitive,
  unstable_useComposerInput,
  useAuiState,
  useAui,
} from "@assistant-ui/react";
import { Button } from "@/components/ui/button";
import type { Attachment } from "@assistant-ui/react";
import { useObjectUrls } from "@/hooks/use-object-urls";
import { resolveTransportImageSrc } from "@/lib/assistant/attachments/image-src";
import { useImageGallery } from "@/components/image/image-gallery";
import { useAttachmentWorkspaceId } from "@/components/conversation/thread/attachment-context";
import { ImageAttachmentStrip } from "@/components/composer/image-attachment-strip";
import { ImageTile } from "@/components/image/image-tile";
import { AttachmentPicker } from "@/components/composer/attachment-picker";
import { useComposerFiles } from "@/components/composer/use-composer-files";
import type { ComposerImageSink } from "@/components/composer/assistant-composer-attachments";
import { useTaskComposerDraft } from "@/components/composer/use-composer-draft";
import { ComposerEditor } from "@/components/composer/composer-editor";


/** 输入框图片组：缩略图与灯箱共用的地址，以及按附件 id 打开灯箱。 */
const ComposerImagesContext = createContext<{ srcById: ReadonlyMap<string, string | undefined>; open: (id: string) => void } | null>(null);

/**
 * 输入框附件条里的一张图片：把 assistant-ui 的附件状态映射成共用缩略图的参数。
 * 普通文件是正文里的行内胶囊，不进附件条；已发送消息里的图片由 UserMessageImages 渲染。
 */
const ComposerImageAttachment: FC = () => {
  const aui = useAui();
  const images = useContext(ComposerImagesContext)!;
  const id = useAuiState((s) => s.attachment.id);
  const name = useAuiState((s) => s.attachment.name);
  const status = useAuiState((s) => s.attachment.status);
  const failed = status.type === "incomplete" && status.reason === "error";
  return (
    <ImageTile
      size="compact"
      src={images.srcById.get(id)}
      alt={name}
      title={name}
      state={status.type === "running" ? "uploading" : failed ? "error" : "ready"}
      errorMessage={failed ? (status.message ?? "上传失败") : undefined}
      onRemove={() => void aui.attachment.remove()}
      onOpen={() => images.open(id)}
    />
  );
};

/** 待发图片的地址：本地文件取对象 URL，已上传的（撤回或编辑排队消息放回输入框的）按附件定位解析。 */
function composerImageSrc(attachment: Attachment, urls: ReadonlyMap<File, string>, workspaceId: number | undefined) {
  if (attachment.file) return urls.get(attachment.file);
  const image = attachment.content?.find((part) => part.type === "image")?.image;
  return image ? resolveTransportImageSrc(image, workspaceId) : undefined;
}

/**
 * 输入框的待发图片条。地址与放大预览在这一层建一次：灯箱能在全部待发图片间翻看，
 * 单张缩略图只经上下文取自己的地址。
 */
export const ComposerAttachments: FC = () => {
  const workspaceId = useAttachmentWorkspaceId();
  const attachments = useAuiState((s) => s.composer.attachments);
  const images = useMemo(() => attachments.filter((attachment) => attachment.type === "image"), [attachments]);
  const urls = useObjectUrls(useMemo(() => images.flatMap((image) => (image.file ? [image.file] : [])), [images]));
  const srcs = useMemo(() => images.map((image) => composerImageSrc(image, urls, workspaceId)), [images, urls, workspaceId]);
  const gallery = useImageGallery(useMemo(
    () => images.map((image, index) => ({ src: srcs[index], alt: image.name })),
    [images, srcs],
  ));
  const context = useMemo(() => ({
    srcById: new Map(images.map((image, index) => [image.id, srcs[index]])),
    open: (id: string) => gallery.open(images.findIndex((image) => image.id === id)),
  }), [gallery.open, images, srcs]);

  return (
    <ComposerImagesContext.Provider value={context}>
      <ImageAttachmentStrip>
        <ComposerPrimitive.Attachments>
          {({ attachment }) => attachment.type === "image" ? <ComposerImageAttachment /> : null}
        </ComposerPrimitive.Attachments>
      </ImageAttachmentStrip>
      {gallery.lightbox}
    </ComposerImagesContext.Provider>
  );
};

/** assistant-ui composer 作为附件入口的图片集合；数量现读 composer 状态，不订阅。 */
function useComposerImageSink(): ComposerImageSink {
  const aui = useAui();
  return {
    add: (file) => aui.composer.addAttachment(file),
    count: () => aui.composer().getState().attachments.filter((attachment) => attachment.type === "image").length,
  };
}

/**
 * 输入框的提交开关：回车与发送按钮共用。
 *
 * 只取 assistant-ui core 的 `composer.canSend`（内容非空、未只读、不在发送中），再叠加调用方的业务
 * 条件 `blocked`；不用 React 层的 `ComposerPrimitive.Send` / `unstable_useComposerInput().canSend`，
 * 因为它们额外按 `thread.isRunning && !capabilities.queue` 禁用——那是「运行中只能靠客户端队列插话」
 * 的模型，而这里运行中发送与编辑排队消息由后端队列承接，能否提交由本应用判定。
 */
function useComposerSubmit(blocked: boolean): { canSubmit: boolean; submit: () => void } {
  const aui = useAui();
  const canSend = useAuiState((state) => state.composer.canSend);
  return {
    canSubmit: canSend && !blocked,
    // assistant-ui 的 send 不返回 Promise，无法在此 await：附件失败由附件适配器
    // （image-attachment-adapter）记录并提示，assistant-ui 自行恢复草稿与发送状态。
    submit: () => {
      void aui.composer.send();
    },
  };
}

/** 发送按钮：与同一输入框的回车提交共用 {@link useComposerSubmit}。 */
export const ComposerSendButton: FC<PropsWithChildren<{
  blocked?: boolean;
  "aria-label": string;
  title?: string;
}>> = ({ blocked = false, "aria-label": ariaLabel, title, children }) => {
  const { canSubmit, submit } = useComposerSubmit(blocked);
  return (
    <Button
      type="button"
      size="icon"
      className="size-8 rounded-full"
      aria-label={ariaLabel}
      title={title}
      disabled={!canSubmit}
      onClick={submit}
    >
      {children}
    </Button>
  );
};

type InlineComposerInputProps = {
  placeholder?: string;
  className?: string;
  autoFocus?: boolean;
  hydrating?: boolean;
  persistTaskDraft?: boolean;
  /** 禁止提交但保留编辑（可选中、复制）；与 {@link ComposerSendButton} 的 `blocked` 传同一个值，回车才不会绕过它。 */
  submitDisabled?: boolean;
  "aria-label"?: string;
};

export const InlineComposerInput: FC<InlineComposerInputProps> = ({
  placeholder,
  className,
  autoFocus,
  hydrating = false,
  persistTaskDraft = false,
  submitDisabled = false,
  "aria-label": ariaLabel,
}) => {
  const composer = unstable_useComposerInput();
  const restoringDraft = useTaskComposerDraft(persistTaskDraft);
  const addFiles = useComposerFiles(useComposerImageSink());
  const { canSubmit, submit } = useComposerSubmit(submitDisabled);

  return (
    <ComposerEditor
      value={composer.value}
      onChange={composer.setText}
      onSubmit={() => {
        if (canSubmit) submit();
      }}
      onExternalFiles={addFiles}
      placeholder={placeholder}
      autoFocus={autoFocus}
      disabled={composer.isDisabled || hydrating || restoringDraft}
      hydrating={hydrating || restoringDraft}
      className={className}
      aria-label={ariaLabel}
    />
  );
};

export const ComposerAttachmentButton: FC<{ workspaceRoot?: string; disabled?: boolean }> = ({ workspaceRoot, disabled = false }) => {
  const images = useComposerImageSink();
  return (
    <AttachmentPicker
      workspaceRoot={workspaceRoot}
      disabled={disabled}
      images={images}
    />
  );
};
