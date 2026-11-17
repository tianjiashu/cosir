import { useState } from "react";

import { AssistantRuntimeErrorBoundary } from "@/components/conversation-runtime/runtime/assistant-runtime-error-boundary";
import { AssistantRuntimeSession } from "@/components/conversation-runtime/runtime/assistant-runtime-session";
import type { AssistantRuntimeProps } from "@/components/conversation-runtime/runtime/runtime-types";
import { useSessionDeliverableWatch } from "@/components/conversation/tools/reply-presentation/use-session-deliverable-watch";

export type { AssistantRuntimeProps } from "@/components/conversation-runtime/runtime/runtime-types";

/**
 * runtime 的渲染隔离：渲染失败时只换掉这棵子树。
 *
 * 连接与 store 由外壳持有，渲染崩溃不影响它们，所以重试不必重读任何东西：换 key 重挂会话，同时请连接重新
 * 同步一次（以防崩溃源自一份异常的状态）。
 */
export function AssistantRuntime(props: AssistantRuntimeProps) {
  const [sessionKey, setSessionKey] = useState(0);
  // 登记在错误边界之外：渲染崩溃重挂会话时监听不断。
  useSessionDeliverableWatch(props.store);
  const retry = () => {
    setSessionKey((key) => key + 1);
    props.connection.resync();
  };
  return (
    <AssistantRuntimeErrorBoundary key={sessionKey} sessionId={props.sessionId} onRetry={retry}>
      <AssistantRuntimeSession {...props} />
    </AssistantRuntimeErrorBoundary>
  );
}
