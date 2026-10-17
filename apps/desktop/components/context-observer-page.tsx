import { useMemo } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { SearchIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState } from "@/components/ui/state-panel";
import { Skeleton } from "@/components/ui/skeleton";
import {
  getContextMessages,
  type ContextMessageQuery,
} from "@/lib/api/context-messages";
import { queryKeys } from "@/lib/query/query-keys";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { QueryBar } from "./context-observer/query-bar";
import { ObserverResults } from "./context-observer/observer-results";
import { parseId } from "./context-observer/message-model";

/** URL 拥有查询范围；服务端记录交给 Query 管理，页面不复制持久化消息事实。 */
export function ContextObserverPage() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const location = useLocation();
  const task = params.get("task_id") ?? "";
  const run = params.get("run_id") ?? "";
  const taskId = parseId(task);
  const runId = parseId(run);
  const query = useMemo<ContextMessageQuery>(
    () => ({ taskId, runId }),
    [taskId, runId],
  );
  const invalid = Boolean((task && !taskId) || (run && !runId));
  const hasQuery = !invalid && (taskId !== undefined || runId !== undefined);
  const scopeKey = `${taskId ?? ""}:${runId ?? ""}`;
  const contextQuery = useQuery({
    queryKey: queryKeys.contextMessages(query),
    queryFn: ({ signal }) => getContextMessages(query, { signal }),
    enabled: hasQuery,
    staleTime: 0,
  });
  const routeState = location.state as {
    returnTo?: unknown;
    currentTaskId?: unknown;
  } | null;
  const returnTo =
    typeof routeState?.returnTo === "string" &&
    routeState.returnTo.startsWith("/") &&
    !routeState.returnTo.startsWith("//")
      ? routeState.returnTo
      : null;
  const currentTaskId =
    typeof routeState?.currentTaskId === "number"
      ? routeState.currentTaskId
      : null;
  const refresh = () => {
    void contextQuery.refetch();
  };
  const changeQuery = (next: ContextMessageQuery, sequence?: number) => {
    if (next.taskId === taskId && next.runId === runId && hasQuery) {
      refresh();
      return;
    }
    const nextParams = new URLSearchParams();
    if (next.taskId !== undefined)
      nextParams.set("task_id", String(next.taskId));
    if (next.runId !== undefined) nextParams.set("run_id", String(next.runId));
    if (sequence !== undefined) nextParams.set("sequence", String(sequence));
    setParams(nextParams, { state: location.state });
  };
  const messages = contextQuery.data;
  const resolvedTaskId = taskId ?? messages?.[0]?.task_id;
  const initialSequenceValue = params.get("sequence");
  const initialSequence =
    initialSequenceValue !== null &&
    /^\d+$/.test(initialSequenceValue) &&
    Number.isSafeInteger(Number(initialSequenceValue))
      ? Number(initialSequenceValue)
      : undefined;
  const error = contextQuery.isError
    ? userErrorMessage(contextQuery.error, "上下文读取失败")
    : null;
  return (
    <div
      className="flex h-full min-h-0 flex-col bg-background"
      data-testid="context-observer"
    >
      <QueryBar
        key={`${task}:${run}`}
        task={task}
        run={run}
        busy={contextQuery.isFetching}
        hasQuery={hasQuery}
        onQuery={changeQuery}
        onRefresh={refresh}
        onBack={returnTo ? () => navigate(returnTo) : undefined}
      />
      {invalid ? (
        <ErrorState
          className="m-5"
          message="查询地址中的 ID 无效，请输入正整数后重新查询。"
        />
      ) : !hasQuery ? (
        <div className="flex min-h-0 flex-1 items-center justify-center p-6">
          <EmptyState
            className="max-w-xl border-0"
            icon={<SearchIcon />}
            title="选择一个任务或轮次，开始检查上下文"
            description="输入 Task ID 查看任务记录，或输入 Run ID 查看本轮记录及系统提示词。记录包含未纳入上下文的消息，不等同于某次模型的完整请求。"
            action={
              currentTaskId ? (
                <Button onClick={() => changeQuery({ taskId: currentTaskId })}>
                  查看当前任务 #{currentTaskId}
                </Button>
              ) : undefined
            }
          />
        </div>
      ) : (
        <>
          <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b px-5 py-2 text-xs">
            <div>
              <span className="font-medium">
                {resolvedTaskId ? `任务 #${resolvedTaskId}` : "任务待解析"}
                {runId ? ` / 轮次 #${runId}` : ""}
              </span>
              {!runId && (
                <span className="ml-2 text-muted-foreground">
                  全部消息记录
                </span>
              )}
            </div>
          </div>
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
          {messages && (
            <ObserverResults
              key={scopeKey}
              messages={messages}
              initialSequence={initialSequence}
              onWholeTask={
                runId && resolvedTaskId
                  ? (sequence) =>
                      changeQuery({ taskId: resolvedTaskId }, sequence)
                  : undefined
              }
            />
          )}
        </>
      )}
    </div>
  );
}
