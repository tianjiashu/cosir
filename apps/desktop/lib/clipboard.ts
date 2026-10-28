import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";

type CopyTextOptions = {
  /** 成功提示的标题，如「已复制内容」。 */
  success: string;
  description?: string;
  /** 失败提示的标题，缺省「复制失败」。 */
  failure?: string;
  /** 失败日志的稳定事件名与业务字段；剪贴板失败多因 WebView 权限或焦点，需要能排查。 */
  logEvent: string;
  logData?: Record<string, unknown>;
};

/**
 * 写入剪贴板并给出全应用一致的反馈：成功提示、失败提示并记 WARNING 日志。
 *
 * 经 Promise 链调用 `writeText`：环境缺少 clipboard API 时的同步 TypeError 也落到失败分支。
 * 终端等自带状态条的组件不走这里，它们在自己的界面里给反馈。
 */
export async function copyText(text: string, options: CopyTextOptions): Promise<boolean> {
  try {
    await Promise.resolve().then(() => navigator.clipboard.writeText(text));
  } catch (error) {
    notify.error(options.failure ?? "复制失败", { error, fallback: "无法写入剪贴板" });
    void frontendLog("WARNING", options.logEvent, "写入剪贴板失败", { data: options.logData, error });
    return false;
  }
  notify.success(options.success, { description: options.description });
  return true;
}
