import {
  InMemoryThreadListAdapter,
  useAssistantTransportRuntime,
  useRemoteThreadListRuntime,
  type RemoteThreadListAdapter,
} from "@assistant-ui/react";
import { useMemo } from "react";

import type { TransportState } from "@/lib/assistant/contract";

type TaskAssistantTransportOptions = Parameters<
  typeof useAssistantTransportRuntime<TransportState>
>[0];

/**
 * Creates the Assistant Transport runtime for one persisted Cosir task.
 *
 * Assistant UI's transport runtime is itself backed by a remote thread-list
 * runtime. The public nesting contract lets the application provide the
 * canonical remote thread identity without copying Assistant UI's run
 * manager. Keeping the adapter stable is important: replacing it reloads the
 * thread list and cancels in-flight mutations in Assistant UI.
 */
export function useTaskAssistantTransportRuntime(
  taskId: number,
  options: TaskAssistantTransportOptions,
) {
  const threadId = `task-${taskId}`;
  const adapter = useMemo<RemoteThreadListAdapter>(() => {
    const inMemory = new InMemoryThreadListAdapter();
    return Object.assign(inMemory, {
      list: async () => ({
        threads: [{ remoteId: threadId, status: "regular" as const }],
      }),
      fetch: async () => ({
        remoteId: threadId,
        status: "regular" as const,
      }),
    });
  }, [threadId]);

  return useRemoteThreadListRuntime({
    adapter,
    threadId,
    allowNesting: true,
    runtimeHook: () => useAssistantTransportRuntime<TransportState>(options),
  });
}
