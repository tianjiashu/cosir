import type { FC, PropsWithChildren } from "react";
import { unstable_useComposerInput, useAuiState, useAui } from "@assistant-ui/react";

import { Button } from "@/components/ui/button";
import { useComposerFiles } from "@/components/composer/use-composer-files";
import type { ComposerImageSink } from "@/components/composer/assistant-composer-attachments";
import { useSessionComposerDraft } from "@/components/composer/use-composer-draft";
import { ComposerEditor } from "@/components/composer/composer-editor";

/** assistant-ui composer 作为附件入口的图片集合（输入框编辑器与「+」菜单共用）；数量现读 composer 状态，不订阅。 */
export function useComposerImageSink(): ComposerImageSink {
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
  persistSessionDraft?: boolean;
  /** 禁止提交但保留编辑（可选中、复制）；与 {@link ComposerSendButton} 的 `blocked` 传同一个值，回车才不会绕过它。 */
  submitDisabled?: boolean;
  "aria-label"?: string;
};

export const InlineComposerInput: FC<InlineComposerInputProps> = ({
  placeholder,
  className,
  autoFocus,
  hydrating = false,
  persistSessionDraft = false,
  submitDisabled = false,
  "aria-label": ariaLabel,
}) => {
  const composer = unstable_useComposerInput();
  const restoringDraft = useSessionComposerDraft(persistSessionDraft);
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
