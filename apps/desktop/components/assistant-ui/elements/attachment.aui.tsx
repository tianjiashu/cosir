"use client";

import type { FC, PropsWithChildren } from "react";
import {
  ComposerPrimitive,
  unstable_useComposerInput,
  useAuiState,
  useAui,
} from "@assistant-ui/react";
import { Button } from "@/components/ui/button";
import { useAttachmentSrc } from "@/hooks/use-attachment-src";
import { useAttachmentWorkspaceId } from "@/components/assistant-ui/elements/attachment-context";
import { frontendLog } from "@/lib/logging/frontend-log";
import { ImageAttachmentCard, ImageAttachmentStrip } from "@/components/composer/image-attachment-card";
import { AttachmentPicker } from "@/components/composer/attachment-picker";
import { useComposerFiles } from "@/components/composer/use-composer-files";
import { useComposerFeedback } from "@/components/composer/composer-feedback";
import { useTaskComposerDraft } from "@/components/composer/use-task-composer-draft";
import { ComposerEditor } from "@/components/composer/composer-editor";


/**
 * 输入框附件条里的一张图片：把 assistant-ui 的附件状态映射成共用缩略图的参数。
 * 普通文件是正文里的行内胶囊，不进附件条；已发送消息里的图片由 UserMessageImages 渲染。
 */
const ComposerImageAttachment: FC = () => {
  const aui = useAui();
  const src = useAttachmentSrc(useAttachmentWorkspaceId());
  const name = useAuiState((s) => s.attachment.name);
  const status = useAuiState((s) => s.attachment.status);
  const failed = status.type === "incomplete" && status.reason === "error";
  return (
    <ImageAttachmentCard
      src={src}
      name={name}
      state={status.type === "running" ? "uploading" : failed ? "error" : "ready"}
      errorMessage={failed ? (status.message ?? "上传失败") : undefined}
      onRemove={() => void aui.attachment.remove()}
    />
  );
};

export const ComposerAttachments: FC = () => {
  return (
    <ImageAttachmentStrip>
      <ComposerPrimitive.Attachments>
        {({ attachment }) => attachment.type === "image" ? <ComposerImageAttachment /> : null}
      </ComposerPrimitive.Attachments>
    </ImageAttachmentStrip>
  );
};

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
