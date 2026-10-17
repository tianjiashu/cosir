import { useEffect, useId } from "react";
import { create } from "zustand";

import { requestConfirmation } from "@/lib/confirm/confirm-store";

/**
 * 未保存修改的统一守卫。
 *
 * 配置页和对话框编辑器用 `useUnsavedChanges` 登记；真正离开并丢弃本地编辑时先确认。
 * 卸载只注销登记；工作台草稿由独立 store/session 持有，使用 workbench/drafts 的销毁确认，
 * 不参与本模块，避免把面板隐藏误当成丢弃。
 */
export type UnsavedScope = "settings" | "dialog";

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
