import { createRoot } from "react-dom/client";
import { useTaskAssistantTransportRuntime } from "@/lib/assistant/use-task-assistant-transport-runtime";
import type { TransportState } from "@/lib/assistant/contract";

/** 真正挂载 adapter，观察提交/活动通知顺序；不向生产代码增加测试接口。 */
export function mountStateCommitProbe(taskId: number, initialState: TransportState) {
  const events: string[] = [];
  let latest = initialState;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  function Probe() {
    useTaskAssistantTransportRuntime(taskId, {
      initialState, traceId: "state-commit-test", connectionKey: "test",
      onStateCommit: state => { latest = state; events.push(`commit:${state.activity?.status ?? "empty"}`); },
      onDisconnect: () => events.push("disconnected"),
      onActivitySettled: status => events.push(`settled:${status}:latest=${latest.activity?.status}`),
    });
    return null;
  }
  root.render(<Probe />);
  return { events, rerender: () => root.render(<Probe />), unmount: () => { root.unmount(); container.remove(); } };
}
