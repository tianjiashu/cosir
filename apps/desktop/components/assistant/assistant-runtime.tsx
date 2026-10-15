"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { TransportStatus, type TransportIssue } from "@/components/assistant/transport-status";
import { TransportErrorDialog } from "@/components/assistant/transport-error-dialog";
import { AssistantRuntimeErrorBoundary } from "@/components/assistant/runtime/assistant-runtime-error-boundary";
import { AssistantRuntimeSession } from "@/components/assistant/runtime/assistant-runtime-session";
import type { AssistantRuntimeProps } from "@/components/assistant/runtime/runtime-types";
import { fetchCanonicalSnapshot } from "@/lib/assistant/canonical-snapshot";
import type { TransportState } from "@/lib/assistant/contract";
import { safeFrontendErrorMessage } from "@/lib/logging/frontend-log";

const RENDER_RECOVERY_SNAPSHOT_TIMEOUT_MS = 15_000;

export type { AssistantRuntimeProps } from "@/components/assistant/runtime/runtime-types";

/**
 * Task 级 Assistant 会话外壳：持有 Transport issue 与渲染错误边界。
 *
 * 两种恢复刻意分开：
 * - 「重新同步」（TransportStatus 按钮）：交给会话内的 recovery 原地对账，
 *   会话、草稿与附件都不卸载。
 * - 渲染失败的重试：错误边界内的树已损坏，只能读取最新快照后以新的 key 重新挂载会话。
 * issue 放在边界外，才能在会话重新挂载时保留。
 */
export function AssistantRuntime(props: AssistantRuntimeProps) {
  const [issue, setIssue] = useState<TransportIssue | null>(null);
  const [sessionKey, setSessionKey] = useState(0);
  const [remountState, setRemountState] = useState<TransportState | null>(null);
  const sessionResyncRef = useRef<(() => void) | null>(null);
  const renderRecoveryAbortRef = useRef<AbortController | null>(null);

  useEffect(() => () => renderRecoveryAbortRef.current?.abort(), []);

  const registerResync = useCallback((resync: (() => void) | null) => {
    sessionResyncRef.current = resync;
  }, []);

  const handleResync = () => {
    // 会话随渲染边界一同存在；边界处于错误态时没有已注册的会话，交给渲染恢复。
    if (sessionResyncRef.current) sessionResyncRef.current();
    else recoverFromRenderError();
  };

  const recoverFromRenderError = () => {
    renderRecoveryAbortRef.current?.abort();
    const controller = new AbortController();
    renderRecoveryAbortRef.current = controller;
    void fetchCanonicalSnapshot(props.taskId, {
      reason: "render_error_recovery",
      timeoutMs: RENDER_RECOVERY_SNAPSHOT_TIMEOUT_MS,
      signal: controller.signal,
    })
      .then((outcome) => {
        if (outcome.kind === "superseded") return;
        setRemountState(outcome.snapshot);
        setIssue(null);
        setSessionKey((key) => key + 1);
      })
      .catch((error: unknown) => {
        setIssue({
          message: safeFrontendErrorMessage(error, "无法读取本机后端的最新对话状态"),
          canResync: true,
        });
      })
      .finally(() => {
        if (renderRecoveryAbortRef.current === controller) renderRecoveryAbortRef.current = null;
      });
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <TransportStatus issue={issue?.presentation === "dialog" ? null : issue} onRetry={handleResync} />
      {issue?.presentation === "dialog" && (
        <TransportErrorDialog
          open
          message={issue.message}
          onOpenChange={(open) => { if (!open) setIssue(null); }}
        />
      )}
      <div className="min-h-0 flex-1">
        <AssistantRuntimeErrorBoundary
          key={sessionKey}
          taskId={props.taskId}
          onRetry={recoverFromRenderError}
        >
          <AssistantRuntimeSession
            {...props}
            initialState={remountState ?? props.initialState}
            setIssue={setIssue}
            registerResync={registerResync}
          />
        </AssistantRuntimeErrorBoundary>
      </div>
    </div>
  );
}
