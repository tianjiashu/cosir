import { useCallback, useMemo } from "react";
import { create } from "zustand";
import type { AgentTeamPreviewDisplay } from "@/components/assistant-ui/tools/agent-team-display";

type Draft = { goal: string; nodeGoals: AgentTeamPreviewDisplay["nodeGoals"]; feedback: string };

/** 未提交的用户输入跨折叠和虚拟卸载保留；不镜像 TeamRun，不持久化到磁盘。 */
const useDrafts = create<{ drafts: Record<string, Draft> }>(() => ({ drafts: {} }));

export function useTeamApprovalDraft(taskId: number, callId: string, preview: AgentTeamPreviewDisplay | null) {
  const key = `${taskId}:${callId}`;
  const initial = useMemo(() => ({ goal: preview?.goal ?? "", nodeGoals: preview?.nodeGoals ?? {}, feedback: "" }), [preview?.goal, preview?.nodeGoals]);
  const stored = useDrafts((state) => state.drafts[key]);
  const update = (patch: Partial<Draft>) => useDrafts.setState((state) => ({ drafts: { ...state.drafts, [key]: { ...(state.drafts[key] ?? initial), ...patch } } }));
  const clear = useCallback(() => useDrafts.setState((state) => {
    if (!(key in state.drafts)) return state;
    const drafts = { ...state.drafts }; delete drafts[key]; return { drafts };
  }), [key]);
  return { draft: stored ?? initial, update, clear };
}

export function forgetTeamApprovalDrafts(taskId: number) {
  useDrafts.setState((state) => ({ drafts: Object.fromEntries(Object.entries(state.drafts).filter(([key]) => !key.startsWith(`${taskId}:`))) }));
}
