import { createJSONStorage, type PersistStorage } from "zustand/middleware";

import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";

/**
 * 渲染进程用户偏好的唯一存储边界：全部偏好 store 经 zustand `persist` 用它落到 localStorage。
 *
 * 约定：key 为 `cosir:<领域>`；结构变化时用 persist 的 `version`，不在 key 上带版本号。读入的值一律由各
 * store 的 `merge` 校验收敛（可能被手改或来自旧版本），这里不解析业务结构。
 *
 * 存储不可用（隐私模式、被禁用、空间不足）只在这一处兜住：读不到当作没有偏好；写不进去记日志并提示一次，
 * 界面照常按本次选择生效，只是下次启动不保留。调用方因此不再各自 try/catch 或判断返回值。
 * 应用是单实例单窗口，不监听跨窗口的 `storage` 事件。
 */
let warned = false;

function reportWriteFailure(name: string, error: unknown): void {
  void frontendLog("WARNING", "preference_storage_write_failed", "偏好写入本机存储失败", { data: { name }, error });
  if (warned) return;
  warned = true;
  notify.error("偏好没有保存", { error, fallback: "本机存储不可用，本次修改在重启后不会保留" });
}

const safeLocalStorage = {
  getItem: (name: string): string | null => {
    try {
      return window.localStorage.getItem(name);
    } catch (error) {
      void frontendLog("WARNING", "preference_storage_read_failed", "读取本机偏好失败，按没有偏好处理", { data: { name }, error });
      return null;
    }
  },
  setItem: (name: string, value: string): void => {
    try {
      window.localStorage.setItem(name, value);
    } catch (error) {
      reportWriteFailure(name, error);
    }
  },
  removeItem: (name: string): void => {
    try {
      window.localStorage.removeItem(name);
    } catch (error) {
      reportWriteFailure(name, error);
    }
  },
};

/** 交给 `persist({ storage })` 的存储；每个 store 各取一份，类型随 store 的持久化部分走。 */
export function preferenceStorage<T>(): PersistStorage<T> {
  return createJSONStorage<T>(() => safeLocalStorage)!;
}
