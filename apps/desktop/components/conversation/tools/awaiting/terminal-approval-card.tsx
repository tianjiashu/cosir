import { useAuiState } from "@assistant-ui/react";
import { InfoIcon, ShieldAlertIcon } from "lucide-react";
import { useState } from "react";

import { ToolIcon } from "@/components/conversation/tools/tool-icons";
import { useOpenSession } from "@/components/links/use-open-session";
import { Button } from "@/components/ui/button";
import { CopyIconButton } from "@/components/ui/copy-icon-button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { PathText } from "@/components/workspace-references/workspace-path-link";
import type { TerminalApproval } from "@/lib/assistant/contract";
import { approvalTarget, commandPending, useCommandRequest } from "@/lib/assistant/command-request-store";
import { decideTerminalApproval } from "@/lib/assistant/session-commands";
import { readRuntimeTransportState } from "@/lib/assistant/transport/transport-state-operations";
import { HttpError } from "@/lib/http/http-error";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { sessionDisplayTitle } from "@/lib/session-title/session-display-title";
import { cn } from "@/lib/utils";
import { AwaitingFooter, AwaitingFrame, AwaitingHeader } from "./awaiting-frame";

const EMPTY_APPROVALS: TerminalApproval[] = [];

/** 升级运行意味着什么：底栏只留一句摘要，全文在提示里（每次都一样，读过一次就不必占版面）。 */
const ESCALATION_SUMMARY = "升级运行：可联网、可写工作区外 · 仅此一次";
const ESCALATION_DETAIL = "这条命令将升级运行：可联网（含 localhost 与监听端口），可写你有权限的任何位置（包括启动项与其他程序）；只有 Cosir 的数据与程序受保护。只对这一次有效。";

/** 命令块悬停才出现复制：分组只在本文件用，按 `reveal-on-hover.ts` 的约定就地写。 */
const REVEAL_ON_COMMAND_HOVER = "opacity-0 group-hover/approval-command:opacity-100 group-has-[:focus-visible]/approval-command:opacity-100 [@media(hover:none)]:opacity-100";

/**
 * 页面上等用户决定的终端批准（snapshot 顶层 `terminal_approvals`），画在对话底部：批准是进程内状态，
 * 可能来自子 Agent 的会话，没有调用位置可依附。卡片只在 snapshot 里有这条请求时渲染，决定后随推送消失。
 */
export function TerminalApprovalCards({ sessionId }: { sessionId: number }) {
  const requests = useAuiState((s) => readRuntimeTransportState(s.thread.state)?.terminal_approvals ?? EMPTY_APPROVALS);
  return <div className="space-y-3">{requests.map((request) => <TerminalApprovalCard key={request.id} sessionId={sessionId} request={request} />)}</div>;
}

/**
 * 一条批准：意图是标题，命令与 Shell、工作目录照原始参数展示（批准界面不得改写参数），风险摘要在底栏。
 * `sessionId` 是显示批准的会话；来源是别的会话（子 Agent）时页头带可打开的来源名。
 */
function TerminalApprovalCard({ sessionId, request }: { sessionId: number; request: TerminalApproval }) {
  const submitting = commandPending(useCommandRequest(sessionId, approvalTarget(request.id)));
  // 只决定「提交中…」写在哪个按钮上；进行中本身以 command-request-store 为准，卸载再挂载后两个按钮照样锁住。
  const [chosen, setChosen] = useState<"approve" | "reject" | null>(null);
  const openSession = useOpenSession();
  const foreign = request.originSessionId !== sessionId;
  const originName = sessionDisplayTitle(request.originTitle);

  const decide = async (decision: "approve" | "reject") => {
    setChosen(decision);
    try {
      await decideTerminalApproval(sessionId, request.id, decision);
    } catch (error) {
      // 409：已决定或已作废（如刚发出的消息撤销了页面上的批准），卡片随 snapshot 消失，不打扰用户。
      if (!(error instanceof HttpError && error.status === 409)) {
        void frontendLog("WARNING", "terminal_approval_decision_failed", "提交终端批准失败", { data: { sessionId, requestId: request.id, decision }, error });
        notify.error("无法提交终端批准", { error, fallback: "请稍后重试" });
      }
    }
  };
  const label = (decision: "approve" | "reject", text: string) => (submitting && chosen === decision ? "提交中…" : text);

  return (
    <AwaitingFrame title="终端执行批准">
      <AwaitingHeader
        icon={<ToolIcon name="terminal" className="size-3.5" aria-hidden="true" />}
        eyebrow={(
          <>
            <span className="shrink-0">终端请求批准</span>
            {foreign && (
              <>
                <span className="text-muted-foreground shrink-0" aria-hidden="true">·</span>
                <button
                  type="button"
                  className="focus-ring min-w-0 truncate rounded-sm underline underline-offset-2 hover:no-underline"
                  aria-label={`打开来源会话：${originName}`}
                  onClick={() => void openSession(request.originSessionId, originName)}
                >
                  {originName}
                </button>
              </>
            )}
          </>
        )}
        title={request.intent || "运行一条命令"}
      />
      <div className="space-y-1.5 px-4 py-3">
        <div className="group/approval-command relative flex items-start gap-2 rounded-md border border-border/60 px-2.5 py-1.5 font-mono text-xs">
          <span className="text-muted-foreground select-none" aria-hidden="true">$</span>
          <pre className="max-h-56 min-w-0 flex-1 overflow-auto font-mono break-all whitespace-pre-wrap">{request.command}</pre>
          <CopyIconButton
            label="复制命令"
            size="xs"
            className={cn("bg-background absolute top-1 right-1", REVEAL_ON_COMMAND_HOVER)}
            getText={() => request.command}
            logEvent="terminal_approval_command_copy_failed"
            logData={{ sessionId, requestId: request.id }}
          />
        </div>
        <div className="text-muted-foreground flex min-w-0 items-center gap-1.5 font-mono text-xs" aria-label={`Shell ${request.shell}，工作目录 ${request.cwd}`}>
          <span className="shrink-0" title={request.shell}>{request.shell}</span>
          <span className="shrink-0" aria-hidden="true">·</span>
          <span className="flex min-w-0" title={request.cwd}><PathText path={request.cwd} /></span>
        </div>
      </div>
      <AwaitingFooter>
        <span className="text-warning flex min-w-0 flex-1 items-center gap-1.5 text-xs">
          <ShieldAlertIcon className="size-3.5 shrink-0" aria-hidden="true" />
          <span className="truncate">{ESCALATION_SUMMARY}</span>
          <Tooltip>
            <TooltipTrigger aria-label="升级运行的完整说明" className="text-muted-foreground hover:text-foreground focus-ring shrink-0 rounded-sm">
              <InfoIcon className="size-3.5" />
            </TooltipTrigger>
            <TooltipContent className="max-w-80">{ESCALATION_DETAIL}</TooltipContent>
          </Tooltip>
        </span>
        <Button type="button" size="sm" variant="outline" disabled={submitting} onClick={() => void decide("reject")}>{label("reject", "拒绝")}</Button>
        <Button type="button" size="sm" disabled={submitting} className={cn(submitting && "cursor-progress")} onClick={() => void decide("approve")}>{label("approve", "允许一次")}</Button>
      </AwaitingFooter>
    </AwaitingFrame>
  );
}
