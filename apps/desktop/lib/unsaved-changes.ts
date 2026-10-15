import { useEffect, useId } from "react";
import { create } from "zustand";

import { requestConfirmation } from "@/lib/confirm/confirm-store";

/**
 * 未保存修改的统一守卫。
 *
 * 编辑器在有未保存内容时用 `useUnsavedChanges` 登记；任何会卸载这些编辑器的离开动作
 * （切换配置标签、返回工作区、关闭工作台标签）先 `await confirmDiscardUnsaved(scope)`。
 * 编辑器卸载即自动注销，因此「放弃」后无需手动清理。
 * scope 只用于区分互不影响的区域：离开配置中心不应询问工作台里的草稿。
 */
export type UnsavedScope = "settings" | "workbench";

type Source = { scope: UnsavedScope; label: string };

const useUnsavedStore = create<{ sources: Record<string, Source> }>(() => ({ sources: {} }));

export function useUnsavedChanges(scope: UnsavedScope, label: string, dirty: boolean): void {
  const id = useId();
  useEffect(() => {
    if (!dirty) return;
    useUnsavedStore.setState((state) => ({ sources: { ...state.sources, [id]: { scope, label } } }));
    return () => {
      useUnsavedStore.setState((state) => {
        const sources = { ...state.sources };
        delete sources[id];
        return { sources };
      });
    };
  }, [dirty, id, label, scope]);
}

/** 当前 scope 内是否有未保存修改（同步读取，供不需要确认框的判断使用）。 */
export function hasUnsavedChanges(scope: UnsavedScope): boolean {
  return Object.values(useUnsavedStore.getState().sources).some((source) => source.scope === scope);
}

/** 若 scope 内有未保存修改则请求确认；没有修改或用户选择放弃时返回 true。 */
export function confirmDiscardUnsaved(scope: UnsavedScope): Promise<boolean> {
  const labels = [...new Set(
    Object.values(useUnsavedStore.getState().sources)
      .filter((source) => source.scope === scope)
      .map((source) => source.label),
  )];
  if (labels.length === 0) return Promise.resolve(true);
  return requestConfirmation({
    title: "放弃未保存的修改？",
    description: `以下内容尚未保存：${labels.join("、")}。离开后这些修改会丢失。`,
    confirmLabel: "放弃修改",
    icon: "discard",
  });
}
