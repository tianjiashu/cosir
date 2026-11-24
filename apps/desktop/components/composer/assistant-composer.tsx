import { useMemo, type FC, type PropsWithChildren } from "react";
import { unstable_useComposerInput, useAuiState, useAui } from "@assistant-ui/react";

import { Button } from "@/components/ui/button";
import { useComposerFiles } from "@/components/composer/use-composer-files";
import type { ComposerImageSink } from "@/components/composer/assistant-composer-attachments";
import { useSessionComposerDraft } from "@/components/composer/use-composer-draft";
import { ComposerEditor } from "@/components/composer/composer-editor";
import { QuoteBar } from "@/components/conversation/quotes/quote-bar";
import { joinQuoteCards, splitQuoteCards } from "@/lib/assistant/composer/quote-cards";

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
    // 消息（含待上传图片）当场离开输入框交给 Session 级提交；上传与 POST 的失败都由提交收回乐观消息、放回输入框。
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
  // 正文里的摘录画成编辑器上方的摘录条，编辑器只拿其余正文；两边的改动都合回同一份正文（见 `quote-cards`）。
  const { quotes, rest } = useMemo(() => splitQuoteCards(composer.value), [composer.value]);
  const disabled = composer.isDisabled || hydrating || restoringDraft;
  const send = () => {
    if (canSubmit) submit();
  };

  return (
    <>
    {quotes.length > 0 && (
      <div data-slot="composer-quotes" className="flex flex-col gap-1.5 pb-2">
        {quotes.map((quote, index) => (
          <QuoteBar
            key={index}
            capsule={quote}
            disabled={disabled}
            onRemove={() => composer.setText(joinQuoteCards(quotes.filter((_, at) => at !== index), rest))}
            onNoteChange={(note) => composer.setText(joinQuoteCards(
              quotes.map((item, at) => (at === index ? { ...item, note: note || undefined } : item)),
              rest,
            ))}
          />
        ))}
      </div>
    )}
    <ComposerEditor
      value={rest}
      onChange={(next) => composer.setText(joinQuoteCards(quotes, next))}
      onAddQuotes={(added, latest) => composer.setText(joinQuoteCards([...quotes, ...added], latest))}
      onSubmit={send}
      onExternalFiles={addFiles}
      placeholder={placeholder}
      autoFocus={autoFocus}
      disabled={disabled}
      hydrating={hydrating || restoringDraft}
      className={className}
      aria-label={ariaLabel}
    />
    </>
  );
};
