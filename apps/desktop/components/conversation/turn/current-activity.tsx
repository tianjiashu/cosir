import { useAuiState } from "@assistant-ui/react";
import { readRuntimeTransportState } from "@/lib/assistant/transport/transport-state-operations";
import { STOPPING_FEEDBACK, unownedActivity } from "@/lib/assistant/turn/current-activity";
import { cn } from "@/lib/utils";
import { CallElapsed } from "../tools/call-elapsed";
import { ActivityStatus, CURRENT_ACTIVITY_SLOT } from "./thinking-label";

/** 当前回复出现前的临时活动行（与活动段同一样式、同一计时）；出现后归 TurnLayout 的活动段，同一时刻只有一个所有者。当前回复即当前活动最后一个可见回合（`activity.turn`）的回复。 */
export function CurrentActivity({ cancelling }: { cancelling: boolean }) {
  const state = useAuiState(s => {
    const state = readRuntimeTransportState(s.thread.state);
    const last = state?.messages.at(-1);
    if (!state || (last?.role === "assistant" && last.turn === state.activity?.turn)) return null;
    return state;
  });
  const result = state && unownedActivity(state.activity, state.background_tasks);
  const feedback = result && cancelling ? STOPPING_FEEDBACK : result;
  return feedback && <div className={cn(CURRENT_ACTIVITY_SLOT, "gap-1.5 px-2 pb-6")}><ActivityStatus feedback={feedback} /><CallElapsed startedAt={feedback.since} /></div>;
}
