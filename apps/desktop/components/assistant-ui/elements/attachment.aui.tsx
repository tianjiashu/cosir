"use client";

import {
  type PropsWithChildren,
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
import { Button } from "@/components/ui/button";
import { useAttachmentSrc } from "@/hooks/use-attachment-src";
import { useAttachmentWorkspaceId } from "@/components/assistant-ui/elements/attachment-context";
import { frontendLog } from "@/lib/logging/frontend-log";
import { cn } from "@/lib/utils";
import { ImageAttachmentCard } from "@/components/composer/image-attachment-card";
import { AttachmentPicker } from "@/components/composer/attachment-picker";
import { useComposerFiles } from "@/components/composer/use-composer-files";
import { useComposerFeedback } from "@/components/composer/composer-feedback";
import { useTaskComposerDraft } from "@/components/composer/use-task-composer-draft";
import { ComposerEditor } from "@/components/composer/composer-editor";


type AttachmentPreviewProps = {
  src: string;
};

const AttachmentPreview: FC<AttachmentPreviewProps> = ({ src }) => {
  const [isLoaded, setIsLoaded] = useState(false);
  return (
    // Local data/blob URLs are generated at runtime.
    <img
      src={src}
      alt="附件预览"
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
  const src = useAttachmentSrc(useAttachmentWorkspaceId());

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
          图片附件预览
        </DialogTitle>
        <div className="aui-attachment-preview bg-background relative mx-auto flex max-h-[80dvh] w-full items-center justify-center overflow-hidden rounded-sm">
          <AttachmentPreview src={src} />
        </div>
      </DialogContent>
    </Dialog>
  );
};

const AttachmentThumb: FC = () => {
  const src = useAttachmentSrc(useAttachmentWorkspaceId());
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
        return "图片";
      case "document":
        return "文档";
      case "file":
        return "文件";
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
      ? (s.attachment.status.message ?? "上传失败")
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
                    "aui-attachment-tile bg-muted hover:after:bg-foreground/10 focus-visible:ring-ring/50 relative size-16 cursor-pointer overflow-hidden rounded-[calc(var(--composer-radius,1.5rem)-var(--composer-padding,8px))] transition-transform outline-none after:pointer-events-none after:absolute after:inset-0 after:rounded-[inherit] after:ring-1 after:ring-foreground/10 after:transition-colors after:ring-inset focus-visible:ring-1 active:scale-[0.96] motion-reduce:transition-none",
                    isError &&
                      "after:ring-destructive/60",
                  )}
                  role="button"
                  tabIndex={0}
                  aria-label={`${typeLabel}附件${
                    isError
                      ? "，上传失败"
                      : isUploading
                        ? "，上传中"
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
          tooltip="移除附件"
          className={cn(
            "aui-attachment-tile-remove size-5 rounded-full active:scale-[0.96] motion-reduce:transition-none",
            compact
              ? "text-muted-foreground hover:bg-foreground/10 hover:text-foreground"
              : "absolute end-1 top-1 bg-foreground/50! text-background after:absolute after:-inset-1.5 hover:bg-foreground/70! hover:text-background!",
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
    submit: () => void aui.composer.send(),
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
  const aui = useAui();
  const addFiles = useComposerFiles((file) => aui.composer.addAttachment(file));
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
  const aui = useAui();
  const feedback = useComposerFeedback();
  const addFiles = useComposerFiles((file) => aui.composer.addAttachment(file));

  return (
    <AttachmentPicker
      workspaceRoot={workspaceRoot}
      disabled={disabled}
      onPicked={(picked) => addFiles(picked.map(({ file }) => file))}
      onError={(message) => {
        feedback.show(message);
        void frontendLog("WARNING", "attachment_picker_rejected", message, { data: {} });
      }}
    />
  );
};
