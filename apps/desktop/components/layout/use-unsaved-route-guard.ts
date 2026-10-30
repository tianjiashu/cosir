import { useEffect, useRef } from "react";
import { useBlocker } from "react-router-dom";
import { confirmDiscardUnsaved, hasUnsavedChanges } from "@/lib/unsaved-changes";

const GUARDED_SCOPES = ["settings", "dialog"] as const;

/**
 * 离开路由前确认丢弃配置页与对话框里的未保存修改；同时覆盖侧栏、命令面板与浏览器前进/后退。
 * 工作台草稿按工作区保存在工作台会话里，切换路由或工作区不会丢弃它们，因此不在这里守卫。
 */
export function useUnsavedRouteGuard() {
  const blocker = useBlocker(({ currentLocation, nextLocation }) => (
    currentLocation.pathname !== nextLocation.pathname && GUARDED_SCOPES.some(hasUnsavedChanges)
  ));
  const handled = useRef(false);
  useEffect(() => {
    if (blocker.state !== "blocked") {
      handled.current = false;
      return;
    }
    if (handled.current) return;
    handled.current = true;
    void (async () => {
      for (const scope of GUARDED_SCOPES) {
        if (!(await confirmDiscardUnsaved(scope))) {
          blocker.reset();
          return;
        }
      }
      blocker.proceed();
    })();
  }, [blocker]);
}
