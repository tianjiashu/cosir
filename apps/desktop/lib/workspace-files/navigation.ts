import { useDockStore } from "@/lib/layout/dock-store";
import { useFilesStore } from "./store";


/**
 * 打开前的异步 info 请求只拥有本次导航意图。新的链接、用户切换文件/搜索或收起面板
 * 都使它过期；读取可以完成，但不得再抢焦点、弹提示或打开旧标签。调用方 finally 释放订阅。
 */
export function beginFileNavigation() {
  const id = useFilesStore.getState().beginNavigation();
  let cancelled = false;
  const stopDock = useDockStore.subscribe((state, previous) => {
    if (state.panel !== previous.panel) cancelled = true;
  });
  return {
    isCurrent: () => !cancelled && id === useFilesStore.getState().navigationId,
    release: () => { stopDock(); },
  };
}
