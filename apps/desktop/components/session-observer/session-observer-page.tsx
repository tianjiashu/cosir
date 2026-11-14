import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { SearchIcon } from "lucide-react";
import { EmptyState, ErrorState } from "@/components/ui/state-panel";
import { Skeleton } from "@/components/ui/skeleton";
import { getSessionObservation } from "@/lib/api/session-observation";
import { queryKeys } from "@/lib/query/query-keys";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { QueryBar } from "./query-bar";
import { ObserverResults } from "./observer-results";
import { parseId } from "./message-model";

/** URL 拥有查询范围；服务端记录交给 Query 管理，页面不复制持久化消息事实。 */
export function SessionObserverPage() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const location = useLocation();
  const session = params.get("session_id") ?? "";
  const turnParam = params.get("turn") ?? "";
  const sessionId = parseId(session);
  const turn = parseId(turnParam);
  const invalid = Boolean((session && !sessionId) || (turnParam && !turn) || (turnParam && !session));
  const scopeKey = `${sessionId ?? ""}:${turn ?? ""}`;
  const observation = useQuery({
    queryKey: queryKeys.sessionObservation({ sessionId, turn }),
    queryFn: ({ signal }) => getSessionObservation({ sessionId: sessionId!, turn }, { signal }),
    enabled: !invalid && sessionId !== undefined,
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
    void observation.refetch();
  };
  const changeTurn = (next: number | undefined) => {
    const nextParams = new URLSearchParams({ session_id: String(sessionId) });
    if (next !== undefined) nextParams.set("turn", String(next));
    setParams(nextParams, { state: location.state });
  };
  const messages = observation.data?.messages;
  const error = observation.isError
    ? userErrorMessage(observation.error, "会话记录读取失败")
    : null;
  return (
    <div
      className="flex h-full min-h-0 flex-col bg-background"
      data-testid="session-observer"
    >
      {invalid ? (
        <ErrorState
          className="m-5"
          message="查询地址无效：Session ID 与回合必须是正整数，按回合查询时要同时给出 Session ID。"
        />
      ) : sessionId === undefined ? (
        <div className="flex min-h-0 flex-1 items-center justify-center p-6">
          <EmptyState
            className="max-w-xl border-0"
            icon={<SearchIcon />}
            title="先打开一个会话，再进入会话观测"
            description="也可以从任意一条回复的操作栏进入，直接查看那一回合。"
          />
        </div>
      ) : (
        <>
          <QueryBar
            sessionId={sessionId}
            title={observation.data?.session_title}
            turn={turn}
            lastTurn={observation.data?.last_turn}
            busy={observation.isFetching}
            onTurn={changeTurn}
            onRefresh={refresh}
            onBack={returnTo ? () => navigate(returnTo) : undefined}
          />
          {error && (
            <ErrorState
              className="mx-5 my-2 shrink-0"
              title={messages ? "刷新失败，当前为上次读取结果" : "读取失败"}
              message={error}
              onRetry={refresh}
            />
          )}
          {!messages && observation.isFetching && (
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
              runs={observation.data?.runs ?? []}
            />
          )}
        </>
      )}
    </div>
  );
}
