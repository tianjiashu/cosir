import { useMemo } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { SearchIcon } from "lucide-react";
import { EmptyState, ErrorState } from "@/components/ui/state-panel";
import { Skeleton } from "@/components/ui/skeleton";
import {
  getContextMessages,
  type ContextMessageQuery,
} from "@/lib/api/context-messages";
import { queryKeys } from "@/lib/query/query-keys";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { QueryBar } from "./query-bar";
import { ObserverResults } from "./observer-results";
import { TurnRunSummary } from "./turn-run-summary";
import { parseId } from "./message-model";

/** URL 拥有查询范围；服务端记录交给 Query 管理，页面不复制持久化消息事实。 */
export function ContextObserverPage() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const location = useLocation();
  const task = params.get("task_id") ?? "";
  const turnParam = params.get("turn") ?? "";
  const taskId = parseId(task);
  const turn = parseId(turnParam);
  const query = useMemo<ContextMessageQuery>(
    () => ({ taskId, turn }),
    [taskId, turn],
  );
  const invalid = Boolean((task && !taskId) || (turnParam && !turn) || (turnParam && !task));
  const hasQuery = !invalid && taskId !== undefined;
  const scopeKey = `${taskId ?? ""}:${turn ?? ""}`;
  const contextQuery = useQuery({
    queryKey: queryKeys.contextMessages(query),
    queryFn: ({ signal }) => getContextMessages(query, { signal }),
    enabled: hasQuery,
    staleTime: 0,
  });
  const routeState = location.state as {
    returnTo?: unknown;
  } | null;
  const returnTo =
    typeof routeState?.returnTo === "string" &&
    routeState.returnTo.startsWith("/") &&
    !routeState.returnTo.startsWith("//")
      ? routeState.returnTo
      : null;
  const refresh = () => {
    void contextQuery.refetch();
  };
  const changeQuery = (next: ContextMessageQuery) => {
    if (next.taskId === taskId && next.turn === turn && hasQuery) {
      refresh();
      return;
    }
    const nextParams = new URLSearchParams();
    if (next.taskId !== undefined)
      nextParams.set("task_id", String(next.taskId));
    if (next.turn !== undefined) nextParams.set("turn", String(next.turn));
    setParams(nextParams, { state: location.state });
  };
  const messages = contextQuery.data?.messages;
  const turnRun = contextQuery.data?.run ?? null;
  const error = contextQuery.isError
    ? userErrorMessage(contextQuery.error, "上下文读取失败")
    : null;
  return (
    <div
      className="flex h-full min-h-0 flex-col bg-background"
      data-testid="context-observer"
    >
      <QueryBar
        key={`${task}:${turnParam}`}
        task={task}
        turn={turnParam}
        busy={contextQuery.isFetching}
        hasQuery={hasQuery}
        onQuery={changeQuery}
        onRefresh={refresh}
        onBack={returnTo ? () => navigate(returnTo) : undefined}
      />
      {invalid ? (
        <ErrorState
          className="m-5"
          message="查询地址无效：Task ID 与回合必须是正整数，按回合查询时要同时给出 Task ID。"
        />
      ) : !hasQuery ? (
        <div className="flex min-h-0 flex-1 items-center justify-center p-6">
          <EmptyState
            className="max-w-xl border-0"
            icon={<SearchIcon />}
            title="选择一个任务（可选回合），开始检查上下文"
          />
        </div>
      ) : (
        <>
          {error && (
            <ErrorState
              className="mx-5 my-2 shrink-0"
              title={messages ? "刷新失败，当前为上次读取结果" : "读取失败"}
              message={error}
              onRetry={refresh}
            />
          )}
          {!messages && contextQuery.isFetching && (
            <div
              role="status"
              aria-label="正在加载消息"
              className="space-y-5 p-5"
            >
              {[0, 1, 2, 3].map((i) => (
                <div key={i} className="max-w-md space-y-2">
                  <Skeleton className="h-4 w-28" />
                  <Skeleton className="h-4 w-full" />
                  <Skeleton className="h-3 w-2/3" />
                </div>
              ))}
            </div>
          )}
          {turnRun && <TurnRunSummary run={turnRun} />}
          {messages && (
            <ObserverResults
              key={scopeKey}
              messages={messages}
            />
          )}
        </>
      )}
    </div>
  );
}
