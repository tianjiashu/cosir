import { useState } from "react";
import { useAui } from "@assistant-ui/react";

import { Button } from "@/components/ui/button";
import { frontendLog } from "@/lib/logging/frontend-log";
import type { NextStepOffer } from "./reply-presentation-display";

/**
 * 回复下方的下一步建议（`offer_next_step`）：一个问题加一到两个按钮，第一个是 Agent 的推荐。
 *
 * 不是等待中的决定：后端没有登记任何东西，点按钮就是替用户把标签原文作为一条普通消息发出去，走输入框
 * 同一条发送路径（乐观消息、模型选择、被拒后的草稿恢复都在 runtime 里）。何时显示由回合决定（只在会话
 * 最后一条回复、Run 正常完成时）；消息一发出这条回复就不再是最后一条，卡片随之消失，`sent` 只用来挡住
 * 这之前的重复点击。
 */
export function NextStepCard({ offer, sessionId }: { offer: NextStepOffer; sessionId: number }) {
  const aui = useAui();
  const [sent, setSent] = useState(false);
  const send = (reply: string, index: number) => {
    setSent(true);
    void frontendLog("INFO", "next_step_reply_sent", "用户点了下一步建议", { data: { sessionId, replyIndex: index } });
    aui.thread().append({ role: "user", content: [{ type: "text", text: reply }] });
  };
  return (
    <section data-slot="next-step-card" aria-label="下一步建议" className="flex flex-wrap items-center gap-x-3 gap-y-2">
      <p className="text-muted-foreground min-w-0 text-2sm">{offer.ask}</p>
      <div className="flex shrink-0 gap-1.5">
        {offer.replies.map((reply, index) => (
          <Button key={reply} type="button" size="sm" variant={index === 0 ? "default" : "outline"} className="h-7 px-2.5" disabled={sent} onClick={() => send(reply, index)}>
            {reply}
          </Button>
        ))}
      </div>
    </section>
  );
}
