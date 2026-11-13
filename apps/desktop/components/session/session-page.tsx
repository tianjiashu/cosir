import { useEffect, useRef, useState } from "react";

import { Assistant } from "@/components/conversation-runtime/assistant";
import { getSession, type WorkspaceSession } from "@/lib/api/workspaces";
import { frontendLog } from "@/lib/logging/frontend-log";
import { TerminalWorkbench } from "@/components/terminal/terminal-workbench";
import { useBackgroundTasksStore } from "@/lib/background-tasks/background-tasks-store";
import { useHandoffStore } from "@/lib/handoff/handoff-store";
import { useTerminalWorkbenchStore } from "@/lib/terminal/terminal-workbench-store";
import { IsolatedRegion } from "@/components/render-isolation/isolated-region";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { SessionWorkspaceLayout } from "@/components/session/session-workspace-layout";
import { ErrorState } from "@/components/ui/state-panel";
import { ThreadSkeleton } from "@/components/conversation-runtime/thread-skeleton";

type SessionProps = {
  sessionId: number;
  initialSession?: WorkspaceSession;
  workspaceRoot?: string;
  forkingMessageId?: string | null;
  onForkMessage?: (messageId: string) => void;
  onSessionLoaded?: (session: WorkspaceSession) => void;
};

/**
 * 会话作用域的 UI session。
 *
 * AppLayout 由路由稳定持有，Session 才随 sessionId 变化。这样会话历史
 * 和 Assistant Transport 可以按会话重新挂载，而 workspace/session 侧栏不会被卸载。
 */
export function SessionPage({
  sessionId,
  initialSession,
  workspaceRoot,
  forkingMessageId,
  onForkMessage,
  onSessionLoaded,
}: SessionProps) {
  const initialSessionId = initialSession?.session_id ?? null;
  const [loadedSession, setLoadedSession] = useState<WorkspaceSession | null>(
    initialSessionId === sessionId ? initialSession ?? null : null,
  );
  const [error, setError] = useState<string | null>(null);
  const [retryToken, setRetryToken] = useState(0);
  const requestGenerationRef = useRef(0);

  useEffect(() => {
    const requestGeneration = ++requestGenerationRef.current;
    const sessionFromSidebar = initialSessionId === sessionId ? initialSession ?? null : null;
    void frontendLog("DEBUG", "session_load_started", "会话页 开始解析会话身份", {
      data: {
        sessionId,
        requestGeneration,
        source: sessionFromSidebar ? "sidebar_cache" : "session_endpoint",
        retryToken,
      },
    });
    if (sessionFromSidebar) {
      setLoadedSession(sessionFromSidebar);
      setError(null);
      onSessionLoaded?.(sessionFromSidebar);
      void frontendLog("DEBUG", "session_loaded_from_sidebar", "会话页 使用侧栏缓存会话", {
        data: { sessionId, workspaceId: sessionFromSidebar.workspace_id, requestGeneration },
      });
      return;
    }

    const controller = new AbortController();
    setLoadedSession(null);
    setError(null);
    void getSession(sessionId, { signal: controller.signal })
      .then((session) => {
        if (controller.signal.aborted || requestGeneration !== requestGenerationRef.current) {
          void frontendLog("DEBUG", "session_response_discarded", "会话页 丢弃过期会话响应", {
            data: { sessionId, requestGeneration, currentGeneration: requestGenerationRef.current, aborted: controller.signal.aborted },
          });
          return;
        }
        setLoadedSession(session);
        onSessionLoaded?.(session);
        void frontendLog("DEBUG", "session_loaded", "会话页 会话加载完成", {
          data: { sessionId, workspaceId: session.workspace_id, requestGeneration },
        });
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted || requestGeneration !== requestGenerationRef.current) {
          void frontendLog("DEBUG", "session_error_discarded", "会话页 丢弃过期会话错误", {
            data: { sessionId, requestGeneration, currentGeneration: requestGenerationRef.current, aborted: controller.signal.aborted },
          });
          return;
        }
        setError(userErrorMessage(cause, "会话加载失败，请重试"));
        void frontendLog("WARNING", "session_load_failed", "会话页 会话加载失败", {
          data: { sessionId, requestGeneration },
          error: cause,
        });
      });
    return () => {
      controller.abort();
      void frontendLog("DEBUG", "session_load_aborted", "会话页 会话请求已 abort", {
        data: { sessionId, requestGeneration },
      });
    };
  // 依赖 initialSessionId（原始 session_id）而非 initialSession 对象引用：父组件在 run 状态
  // 切换等无关更新时会重建 activeSession 对象引用，若依赖整对象会触发冗余重渲染链。
  }, [initialSessionId, onSessionLoaded, retryToken, sessionId]);

  // 终端会话镜像只在本 Session 页存在期间有效：离开时清空，回来时等新 snapshot 再挂载窗格（见 store 说明）。
  useEffect(() => () => {
    useTerminalWorkbenchStore.getState().syncSessions(sessionId, null);
    useBackgroundTasksStore.getState().sync(sessionId, null);
    useHandoffStore.getState().sync(sessionId, null);
  }, [sessionId]);

  if (error) return (
    <div className="flex h-full items-center justify-center p-6">
      <ErrorState className="w-full max-w-md" message={error} onRetry={() => setRetryToken((value) => value + 1)} />
    </div>
  );
  // 与 Assistant 等订阅首帧的阶段共用同一骨架，两段加载在视觉上连续。
  if (!loadedSession || loadedSession.session_id !== sessionId) return <ThreadSkeleton />;

  return (
    <SessionWorkspaceLayout bottomPanel={(
      // 终端面板崩溃只降级面板本身，不能连带卸载正在运行的 Assistant 会话。
      <IsolatedRegion
        region="terminal-panel"
        label="终端面板"
        resetKey={`terminal:${sessionId}`}
        metadata={{ sessionId }}
        fallbackClassName="dock-panel bg-background mx-2 mb-2 shrink-0 p-3"
        layout="row"
        // 先关闭再重置边界：重新挂载的 Host 因面板已关闭而不渲染，不会再次触发同一崩溃。
        escape={{ label: "收起终端", onClick: (reset) => { useTerminalWorkbenchStore.getState().setOpen(sessionId, false); reset(); } }}
      >
        <TerminalWorkbench sessionId={sessionId} />
      </IsolatedRegion>
    )}>
      <div className="h-full min-h-0 min-w-0 w-full">
        <Assistant
          sessionId={sessionId}
          workspaceId={loadedSession.workspace_id}
          workspaceRoot={workspaceRoot}
          forkingMessageId={forkingMessageId}
          onForkMessage={onForkMessage}
        />
      </div>
    </SessionWorkspaceLayout>
  );
}
