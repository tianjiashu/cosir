"use client";

import {
  type PropsWithChildren,
  useMemo,
  useState,
  type FC,
  isValidElement,
} from "react";
import {
  XIcon,
  FileText,
  Loader2Icon,
  AlertCircleIcon,
} from "lucide-react";
import {
  AttachmentPrimitive,
  ComposerPrimitive,
  unstable_useComposerInput,
  useAuiState,
  useAui,
} from "@assistant-ui/react";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogTrigger,
} from "@/components/ui/dialog";
import { TooltipIconButton } from "@/components/tooltip-icon-button";
import { useAttachmentSrc } from "@/hooks/use-attachment-src";
import { useAttachmentTaskId } from "@/components/assistant-ui/elements/attachment-context";
import { frontendLog } from "@/lib/logging/frontend-log";
import { cn } from "@/lib/utils";
import { ImageAttachmentCard } from "@/components/composer/image-attachment-card";
import { AttachmentPicker, type PickedComposerAttachment } from "@/components/composer/attachment-picker";
import {
  InlineAttachmentInput,
  useInlineComposerInsertion,
  type InlineFileAttachment,
} from "@/components/composer/inline-attachment-input";
import { inlineAttachmentTokenId } from "@/lib/assistant/attachments/local-file-token";

type AttachmentPreviewProps = {
  src: string;
};

const AttachmentPreview: FC<AttachmentPreviewProps> = ({ src }) => {
  const [isLoaded, setIsLoaded] = useState(false);
  return (
    // Local data/blob URLs are generated at runtime.
    <img
      src={src}
      alt="Attachment preview"
      className={cn(
        "block h-auto max-h-[80vh] w-auto max-w-full rounded-sm object-contain transition-opacity duration-300 motion-reduce:transition-none",
        isLoaded
          ? "aui-attachment-preview-image-loaded opacity-100"
          : "aui-attachment-preview-image-loading opacity-0",
      )}
      onLoad={() => setIsLoaded(true)}
    />
  );
};

const AttachmentPreviewDialog: FC<PropsWithChildren> = ({ children }) => {
  const src = useAttachmentSrc(useAttachmentTaskId());

  if (!src) return children;

  return (
    <Dialog>
      <DialogTrigger
        nativeButton={false}
        className="aui-attachment-preview-trigger cursor-zoom-in"
        render={
          isValidElement(children) ? (
            children
          ) : (
            <button type="button">{children}</button>
          )
        }
      />
      <DialogContent className="aui-attachment-preview-dialog-content [&>button]:bg-foreground/60 [&>button]:hover:bg-foreground/80 [&_svg]:text-background p-2 sm:max-w-3xl [&>button]:rounded-full [&>button]:p-1 [&>button]:opacity-100 [&>button]:ring-0!">
        <DialogTitle className="aui-sr-only sr-only">
          Image Attachment Preview
        </DialogTitle>
        <div className="aui-attachment-preview bg-background relative mx-auto flex max-h-[80dvh] w-full items-center justify-center overflow-hidden rounded-sm">
          <AttachmentPreview src={src} />
        </div>
      </DialogContent>
    </Dialog>
  );
};

const AttachmentThumb: FC = () => {
  const src = useAttachmentSrc(useAttachmentTaskId());
  return <ImageAttachmentCard src={src} name="图片附件" className="aui-attachment-tile-avatar rounded-none border-0 shadow-none" />;
};

const AttachmentUI: FC = () => {
  const aui = useAui();
  const isComposer = aui.attachment.source !== "message";

  const isImage = useAuiState((s) => s.attachment.type === "image");
  const typeLabel = useAuiState((s) => {
    const type = s.attachment.type;
    switch (type) {
      case "image":
        return "Image";
      case "document":
        return "Document";
      case "file":
        return "File";
      default:
        return type;
    }
  });

  const uploadState = useAuiState((s) =>
    s.attachment.status.type === "running"
      ? "uploading"
      : s.attachment.status.type === "incomplete" &&
          s.attachment.status.reason === "error"
        ? "error"
        : undefined,
  );
  const isUploading = uploadState === "uploading";
  const isError = uploadState === "error";

  const errorMessage = useAuiState((s) =>
    s.attachment.status.type === "incomplete" &&
    s.attachment.status.reason === "error"
      ? (s.attachment.status.message ?? "Upload failed")
      : undefined,
  );

  if (!isImage) {
    return (
      <AttachmentPrimitive.Root
        className={cn(
          "aui-attachment-file-chip bg-muted/70 text-foreground inline-flex max-w-64 items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs",
          isError && "border-destructive/50 text-destructive",
        )}
        title={errorMessage}
      >
        {isUploading ? (
          <Loader2Icon className="text-muted-foreground size-3.5 shrink-0 animate-spin" />
        ) : (
          <FileText className="text-muted-foreground size-3.5 shrink-0" />
        )}
        <span className="min-w-0 truncate font-medium"><AttachmentPrimitive.Name /></span>
        {isComposer && <AttachmentRemove compact />}
      </AttachmentPrimitive.Root>
    );
  }

  return (
    <TooltipProvider>
      <Tooltip>
        <AttachmentPrimitive.Root
          className={cn(
            "aui-attachment-root relative",
            isComposer &&
              "h-16 min-h-16 w-16 min-w-16 shrink-0 animate-in fade-in-0 zoom-in-95 duration-200 motion-reduce:animate-none",
            isImage &&
              !isComposer &&
              "aui-attachment-root-message only:*:first:size-24",
          )}
        >
          <AttachmentPreviewDialog>
            <TooltipTrigger
              render={
                <div
                  className={cn(
                    "aui-attachment-tile bg-muted hover:after:bg-foreground/10 focus-visible:ring-ring/50 relative size-16 cursor-pointer overflow-hidden rounded-[calc(var(--composer-radius,1.5rem)-var(--composer-padding,8px))] transition-transform outline-none after:pointer-events-none after:absolute after:inset-0 after:rounded-[inherit] after:ring-1 after:ring-black/10 after:transition-colors after:ring-inset focus-visible:ring-1 active:scale-[0.96] motion-reduce:transition-none dark:after:ring-white/10",
                    isError &&
                      "after:ring-destructive/60 dark:after:ring-destructive/60",
                  )}
                  role="button"
                  tabIndex={0}
                  aria-label={`${typeLabel} attachment${
                    isError
                      ? ", upload failed"
                      : isUploading
                        ? ", uploading"
                        : ""
                  }`}
                />
              }
            >
              <AttachmentThumb />
              {isUploading && (
                <div
                  aria-hidden="true"
                  className="aui-attachment-tile-uploading bg-background/60 animate-in fade-in-0 absolute inset-0 flex items-center justify-center backdrop-blur-[2px] motion-reduce:animate-none"
                >
                  <Loader2Icon className="text-muted-foreground size-4 animate-spin" />
                </div>
              )}
              {isError && (
                <div
                  aria-hidden="true"
                  className="aui-attachment-tile-error bg-background/70 animate-in fade-in-0 absolute inset-0 flex items-center justify-center backdrop-blur-[2px] motion-reduce:animate-none"
                >
                  <AlertCircleIcon className="text-destructive size-4" />
                </div>
              )}
            </TooltipTrigger>
          </AttachmentPreviewDialog>
          {isComposer && <AttachmentRemove />}
        </AttachmentPrimitive.Root>
        <TooltipContent side="top">
          <AttachmentPrimitive.Name />
          {errorMessage && (
            <p className="aui-attachment-error-message">{errorMessage}</p>
          )}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
};

const AttachmentRemove: FC<{ compact?: boolean }> = ({ compact = false }) => {
  return (
    <AttachmentPrimitive.Remove
      render={
        <TooltipIconButton
          tooltip="Remove file"
          className={cn(
            "aui-attachment-tile-remove size-5 rounded-full active:scale-[0.96] motion-reduce:transition-none",
            compact
              ? "text-muted-foreground hover:bg-foreground/10 hover:text-foreground"
              : "absolute end-1 top-1 bg-black/50! text-white after:absolute after:-inset-1.5 hover:bg-black/70! hover:text-white!",
          )}
          side="top"
        />
      }
    >
      <XIcon className="aui-attachment-remove-icon size-3 stroke-[2.5]" />
    </AttachmentPrimitive.Remove>
  );
};

export const ComposerAttachments: FC = () => {
  return (
    <div className="aui-composer-attachments flex h-16 min-h-16 max-h-16 min-w-0 w-full shrink-0 flex-row items-center gap-2 overflow-x-auto overflow-y-hidden empty:hidden">
      <ComposerPrimitive.Attachments>
        {({ attachment }) => attachment.type === "image" ? <AttachmentUI /> : null}
      </ComposerPrimitive.Attachments>
    </div>
  );
};

export const UserMessageFilePart: FC<{
  filename: string;
  mimeType: string;
}> = ({ filename, mimeType }) => (
  <span
    data-slot="user-message-file-part"
    data-content-type={mimeType}
    className="bg-muted/70 text-foreground inline-flex max-w-64 items-center gap-1.5 rounded-lg border px-2.5 py-1.5 align-middle text-xs"
    title={filename}
  >
    <FileText className="text-muted-foreground size-3.5 shrink-0" />
    <span className="min-w-0 truncate font-medium">{filename}</span>
  </span>
);

type InlineComposerInputProps = {
  placeholder?: string;
  className?: string;
  autoFocus?: boolean;
  suspendAttachmentReconciliation?: boolean;
  "aria-label"?: string;
};

export const InlineComposerInput: FC<InlineComposerInputProps> = ({
  placeholder,
  className,
  autoFocus,
  suspendAttachmentReconciliation = false,
  "aria-label": ariaLabel,
}) => {
  const composer = unstable_useComposerInput();
  const aui = useAui();
  const attachments = useAuiState((state) => state.composer.attachments);
  const fileAttachments = useMemo(
    () => attachments
      .filter((attachment) => attachment.type !== "image")
      .map<InlineFileAttachment>((attachment) => ({
        id: attachment.id,
        name: attachment.name,
        kind: "file",
        // 与文本 token 共用同一身份归一规则，避免「同步中」被误判为「已失效」。
        tokenId: inlineAttachmentTokenId(attachment),
      })),
    [attachments],
  );
  /**
   * 移除附件列表中的普通文件附件（内联 token 的 × 会经此转到附件对象）。
   *
   * 副作用：同步读取 composer 状态；命中时派发异步 `remove()`，不等待结果。
   * 显式移除是用户意图，不做静默忽略——草稿同步期间输入框本身处于禁用态，无需在此二次
   * 拦截；找不到附件时记日志，便于复盘「点了 × 没反应」。
   */
  const removeAttachment = (fileId: string) => {
    const attachments = aui.composer.getState().attachments;
    const attachmentIndex = attachments.findIndex((attachment) => attachment.id === fileId);
    if (attachmentIndex < 0) {
      void frontendLog("WARNING", "inline_attachment_remove_missing", "附件列表中不存在待移除附件", {
        data: { fileId, attachmentIds: attachments.map((attachment) => attachment.id) },
      });
      return;
    }
    void aui.composer.attachment({ index: attachmentIndex }).remove();
    void frontendLog("DEBUG", "inline_attachment_remove_dispatched", "已派发附件移除", {
      data: {
        fileId,
        attachmentIndex,
        attachmentType: attachments[attachmentIndex]?.type ?? null,
        remainingAttachmentIds: attachments
          .filter((_, index) => index !== attachmentIndex)
          .map((attachment) => attachment.id),
      },
    });
  };

  const addExternalFiles = async (files: readonly File[]): Promise<InlineFileAttachment[]> => {
    const existingIds = new Set(aui.composer.getState().attachments.map((attachment) => attachment.id));
    const insertedFiles: InlineFileAttachment[] = [];
    for (const file of files) {
      await aui.composer.addAttachment(file);
      const added = aui.composer.getState().attachments.find(
        (attachment) => attachment.file === file && !existingIds.has(attachment.id),
      );
      if (!added) continue;
      existingIds.add(added.id);
      if (added.type !== "image") {
        insertedFiles.push({
          id: added.id,
          name: added.name,
          kind: "file",
          tokenId: inlineAttachmentTokenId(added),
        });
      }
    }
    return insertedFiles;
  };

  return (
    <InlineAttachmentInput
      value={composer.value}
      onChange={composer.setText}
      onSubmit={() => composer.send()}
      attachments={fileAttachments}
      onExternalFiles={addExternalFiles}
      onRemoveAttachment={removeAttachment}
      placeholder={placeholder}
      autoFocus={autoFocus}
      disabled={composer.isDisabled || suspendAttachmentReconciliation}
      suspendAttachmentReconciliation={suspendAttachmentReconciliation}
      className={className}
      aria-label={ariaLabel}
    />
  );
};

export const ComposerAttachmentButton: FC<{ workspaceRoot?: string; disabled?: boolean }> = ({ workspaceRoot, disabled = false }) => {
  const aui = useAui();
  const insertion = useInlineComposerInsertion();

  const addPicked = async (picked: PickedComposerAttachment[]) => {
    const existingIds = new Set(aui.composer.getState().attachments.map((attachment) => attachment.id));
    const insertedFiles: InlineFileAttachment[] = [];
    for (const attachment of picked) {
      if (existingIds.has(attachment.id)) continue;
      await aui.composer.addAttachment(attachment.file);
      existingIds.add(attachment.id);
      if (attachment.kind === "file") {
        insertedFiles.push({ id: attachment.id, name: attachment.name, kind: "file" });
      }
    }
    insertion.insert(insertedFiles);
  };

  return (
    <AttachmentPicker
      workspaceRoot={workspaceRoot}
      disabled={disabled}
      onPicked={addPicked}
      onError={(message) => void frontendLog("WARNING", "attachment_picker_rejected", message, { data: {} })}
    />
  );
};
