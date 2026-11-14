import { useEffect, useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";

import { NewConversationFrame } from "@/components/new-conversation/new-conversation-frame";
import { ErrorState } from "@/components/ui/state-panel";
import { Skeleton } from "@/components/ui/skeleton";
import { getOrCreateUnstartedSession, type Workspace, type WorkspaceSession } from "@/lib/api/workspaces";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { frontendLog } from "@/lib/logging/frontend-log";

/**
 * 从 `/` 转到未开始的会话时随导航带上的取或建结果：目录推送还没到，会话页与新会话版面先用它，
 * 不必再请求一次 `GET /sessions/:id`。只在这一次导航里有效，之后以目录为准。
 */
export type NewConversationRouteState = { session: WorkspaceSession };

type NewConversationProps = {
  workspaces: Workspace[];
  /** 目录首次加载完成前没有选中的工作区并不代表用户没有工作区，此时只显示占位。 */
  catalogLoading: boolean;
  selectedWorkspaceId: number | null;
  /** 所选工作区未开始的会话（目录里已有时直接转过去，不再请求）。 */
  unstartedSession: WorkspaceSession | null;
  onWorkspaceChange: (workspaceId: number) => void;
  /** 从文件夹添加工作区（与侧栏共用 `useWorkspaceActions.addWorkspaceFromFolder`）。 */
  onAddWorkspace: () => Promise<boolean>;
  addingWorkspace: boolean;
};

/**
 * 新会话页的路由（`/`）：只负责把它解析成所选工作区未开始的会话并转过去。
 *
 * 新会话页本身就是那个会话（会话页套 `NewConversationFrame`，见 AppLayout），输入、草稿、模型选择、终端
 * 与浏览器标签都是会话页的那一套。这里只剩不对应会话的状态：目录加载中、没有或没选工作区、正在取或建、
 * 取或建失败。取或建是幂等的（每个工作区至多一个），重复进入或 StrictMode 下的两次请求得到同一个会话。
 */
export function NewConversation({
  workspaces,
  catalogLoading,
  selectedWorkspaceId,
  unstartedSession,
  onWorkspaceChange,
  onAddWorkspace,
  addingWorkspace,
}: NewConversationProps) {
  const navigate = useNavigate();
  const [failure, setFailure] = useState<{ workspaceId: number; message: string } | null>(null);
  const [retryToken, setRetryToken] = useState(0);
  const needsSession = selectedWorkspaceId !== null && unstartedSession === null;

  useEffect(() => {
    if (!needsSession || selectedWorkspaceId === null) return;
    // 卸载即用户已离开 `/`：迟到的结果不再导航。
    let left = false;
    setFailure(null);
    getOrCreateUnstartedSession(selectedWorkspaceId)
      .then((session) => {
        if (left) return;
        navigate(`/sessions/${session.session_id}`, { replace: true, state: { session } satisfies NewConversationRouteState });
      })
      .catch((cause: unknown) => {
        void frontendLog("ERROR", "unstarted_session_resolve_failed", "新会话页取或建未开始的会话失败", {
          data: { workspaceId: selectedWorkspaceId },
          error: cause,
        });
        if (!left) setFailure({ workspaceId: selectedWorkspaceId, message: userErrorMessage(cause, "新会话打开失败，请重试") });
      });
    return () => { left = true; };
  }, [navigate, needsSession, retryToken, selectedWorkspaceId]);

  if (unstartedSession) return <Navigate to={`/sessions/${unstartedSession.session_id}`} replace />;

  const failed = failure !== null && failure.workspaceId === selectedWorkspaceId ? failure.message : null;
  return (
    <NewConversationFrame
      workspaces={workspaces}
      selectedWorkspaceId={selectedWorkspaceId}
      onWorkspaceChange={onWorkspaceChange}
      onAddWorkspace={onAddWorkspace}
      addingWorkspace={addingWorkspace}
    >
      {failed ? (
        <ErrorState message={failed} onRetry={() => setRetryToken((value) => value + 1)} />
      ) : catalogLoading || needsSession ? (
        // 与会话页的加载骨架同一形状：转到会话页时输入区位置不跳。
        <Skeleton role="status" aria-label="正在打开新会话" className="h-28 w-full rounded-3xl" />
      ) : (
        <p className="border-border text-muted-foreground flex h-28 items-center justify-center rounded-3xl border border-dashed text-sm">
          {workspaces.length > 0 ? "先在上方选择一个工作区" : "先在上方添加一个工作区"}
        </p>
      )}
    </NewConversationFrame>
  );
}
