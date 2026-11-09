import { useEffect, useId } from "react";
import { create } from "zustand";

import { confirmDiscard } from "@/lib/confirm/confirm-store";

/**
 * 配置页未保存修改的统一守卫。
 *
 * 配置页编辑器用 `useUnsavedChanges` 登记；离开路由（`use-unsaved-route-guard`）或页内放弃编辑时先确认。
 * 卸载只注销登记；工作台草稿由独立 store/session 持有，使用 workbench/drafts 的销毁确认，
 * 不参与本模块，避免把面板隐藏误当成丢弃。
 */
const useUnsavedStore = create<{ sources: Record<string, string> }>(() => ({ sources: {} }));

export function useUnsavedChanges(label: string, dirty: boolean): void {
  const id = useId();
  useEffect(() => {
    if (!dirty) return;
    useUnsavedStore.setState((state) => ({ sources: { ...state.sources, [id]: label } }));
    return () => {
      useUnsavedStore.setState((state) => {
        const sources = { ...state.sources };
        delete sources[id];
        return { sources };
      });
    };
  }, [dirty, id, label]);
}

/** 是否有未保存修改（同步读取，供不需要确认框的判断使用）。 */
export function hasUnsavedChanges(): boolean {
  return Object.keys(useUnsavedStore.getState().sources).length > 0;
}

/** 有未保存修改时请求确认；没有修改或用户选择放弃时返回 true。 */
export function confirmDiscardUnsaved(): Promise<boolean> {
  const labels = [...new Set(Object.values(useUnsavedStore.getState().sources))];
  if (labels.length === 0) return Promise.resolve(true);
  return confirmDiscard({
    title: "放弃未保存的修改？",
    description: `以下内容尚未保存：${labels.join("、")}。离开后这些修改会丢失。`,
    confirmLabel: "放弃修改",
  });
}
