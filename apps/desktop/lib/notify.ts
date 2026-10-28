import { toast } from "sonner";

import { userErrorMessage } from "@/lib/errors/user-error-message";

/**
 * 一次性操作结果的唯一通知通道（toast）。
 *
 * 呈现规则（全应用统一，新增页面照此选择）：
 * - 与当前表单 / 区块内容直接相关、用户需要对照修改的错误：在原位用 `<Alert>`。
 * - 一次性操作的结果（保存成功、删除完成）与后台动作的失败（停止、继续运行、上传、分叉）：
 *   用这里的 toast；失败可以附带「重试」操作。
 * - 加载失败、空数据等区块状态：用 `LoadingState` / `EmptyState` / `ErrorState`。
 * 本模块只负责呈现；诊断日志仍由调用方在业务链路上用 frontendLog 记录。
 */

type NotifyAction = { label: string; onClick: () => void };

export const notify = {
  success(message: string, options?: { description?: string; action?: NotifyAction }) {
    toast.success(message, { description: options?.description, action: options?.action });
  },

  /**
   * 失败提示。传入原始错误时经 userErrorMessage 转换为可展示文案；
   * `title` 说明哪个动作失败，文案本身作为描述。
   */
  error(title: string, options?: { error?: unknown; fallback?: string; action?: NotifyAction }) {
    const description = options?.error === undefined
      ? undefined
      : userErrorMessage(options.error, options.fallback ?? "请稍后重试");
    toast.error(title, {
      description,
      action: options?.action,
      // 失败需要用户读完并决定是否重试，停留时间长于成功提示。
      duration: 8_000,
    });
  },
};
