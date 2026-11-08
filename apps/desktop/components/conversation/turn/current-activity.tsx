import { useAuiState } from "@assistant-ui/react";
import { readRuntimeTransportState } from "@/lib/assistant/transport/transport-state-operations";
import { currentActivity, STOPPING_FEEDBACK } from "@/lib/assistant/turn/current-activity";
import { ActivityStatus } from "./thinking-label";

/** 当前回复出现前的临时槽位；出现后归 TurnLayout，同一时刻只有一个所有者。当前回复即当前活动最后一个可见回合（`activity.turn`）的回复。 */
export function CurrentActivity({ cancelling }: { cancelling: boolean }) {
  const state = useAuiState(s => {
    const state = readRuntimeTransportState(s.thread.state);
    const last = state?.messages.at(-1);
    if (!state || (last?.role === "assistant" && last.turn === state.activity?.turn)) return null;
    return state;
  });
  const result = state && currentActivity(state.activity, [], state.background_tasks);
  const feedback = result && cancelling ? STOPPING_FEEDBACK : result;
  return feedback && <ActivityStatus feedback={feedback} className="block px-2 pb-6 text-2sm" />;
}
