import { MessageSquareWarningIcon } from "lucide-react";
import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { BrowserDialog, CosirHost } from "@/electron/shared/host-contract";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";

const TITLES: Record<BrowserDialog["type"], string> = { alert: "网页提示", confirm: "网页请你确认", prompt: "网页请你输入" };

/**
 * 标签上待答的 JS 弹窗：网页内容上方的提示条，与交接提示同一摆法（在原生画面占位区之外，不会被网页盖住）。
 *
 * 页面在作答前一直停着，所以这是常显的「等你操作」状态，不用弹层。alert 只有「确定」，confirm 有「确定」
 * 「取消」，prompt 带预填默认值的输入框。用户与 Agent（`browser_act` 的 `dialog` 动作）都能作答、先到的生效；
 * 作答带弹窗 id，它已不是当前弹窗时宿主忽略，过时的点击不会答给紧接着弹出的新弹窗。作答后宿主推送
 * `dialog: null` 或新弹窗，提示条随之消失或更换，这里不做乐观收起。调用方以弹窗 id 作 `key`，换一个弹窗即
 * 重置输入框。
 */
export function BrowserDialogBanner({ host, tabId, dialog }: { host: CosirHost; tabId: string; dialog: BrowserDialog }) {
  const [text, setText] = useState(dialog.defaultText);
  const [answering, setAnswering] = useState(false);

  const answer = (accept: boolean) => {
    setAnswering(true);
    const reply = dialog.type === "prompt" && accept ? { accept, text } : { accept };
    host.browser.answerDialog(tabId, dialog.id, reply).then(
      () => {
        void frontendLog("INFO", "browser_dialog_answered_by_user", "用户回答了网页弹窗", { data: { tabId, dialogId: dialog.id, type: dialog.type, accept } });
      },
      (error: unknown) => {
        void frontendLog("WARNING", "browser_dialog_answer_failed", "回答网页弹窗失败", { data: { tabId, dialogId: dialog.id, type: dialog.type }, error });
        notify.error("回答网页弹窗失败", { error });
      },
    ).finally(() => setAnswering(false));
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    answer(true);
  };

  return (
    <form
      role="alert"
      aria-label={TITLES[dialog.type]}
      data-testid="browser-dialog-banner"
      className="bg-info/10 text-foreground mx-2 mb-1 flex flex-col gap-1.5 rounded-md px-3 py-1.5 text-xs"
      onSubmit={submit}
    >
      <div className="flex min-w-0 items-start gap-2">
        <MessageSquareWarningIcon className="text-info mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
        <p className="line-clamp-4 min-w-0 flex-1 break-words whitespace-pre-wrap" title={dialog.message}>
          <span className="text-muted-foreground">{TITLES[dialog.type]}：</span>
          {dialog.message || "（没有内容）"}
        </p>
      </div>
      <div className="flex items-center justify-end gap-1.5">
        {dialog.type === "prompt" && (
          <Input
            aria-label="弹窗输入"
            className="h-6 min-w-0 flex-1 text-xs"
            value={text}
            spellCheck={false}
            disabled={answering}
            onChange={(event) => setText(event.target.value)}
          />
        )}
        {dialog.type !== "alert" && (
          <Button type="button" size="xs" variant="ghost" disabled={answering} onClick={() => answer(false)}>取消</Button>
        )}
        <Button type="submit" size="xs" disabled={answering}>确定</Button>
      </div>
    </form>
  );
}
