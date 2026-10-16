import { useStore } from "zustand";
import type { TeamEditorSession } from "@/lib/agent-team-editor/session";
import { useUnsavedChanges, type UnsavedScope } from "@/lib/unsaved-changes";

/** 守卫跟随会话所有者挂载，隐藏或切换画布不会注销未保存内容。 */
export function TeamEditorUnsavedGuard({ session, scope }: { session: TeamEditorSession; scope: UnsavedScope }) {
  const dirty = useStore(session.store, state => state.dirty);
  useUnsavedChanges(scope, "Agent Team 配置", dirty);
  return null;
}
