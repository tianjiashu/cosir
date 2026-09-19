"use client";

import { useEffect, useRef, useState } from "react";
import { frontendLog } from "@/lib/logging/frontend-log";

export type UseCopyToClipboardOptions = {
  copiedDuration?: number;
};

/**
 * 复制文本到系统剪贴板，并暴露一个会自动复位的 `isCopied` 高亮状态。
 *
 * 复制成功后 `isCopied` 置为 true，并在 `copiedDuration` 毫秒后自动复位为 false。
 * 复用同一定时器句柄：快速重复复制会重置倒计时而非叠加复位，避免高亮态来回跳动；
 * 组件卸载时清理待触发的定时器，避免卸载后调用 `setIsCopied` 触发 React 警告。
 * 复制失败（如浏览器拒绝授权或运行环境无 clipboard API）时 `isCopied` 保持 false，
 * 并写入 WARNING 日志，不向上抛异常。
 *
 * @param copiedDuration `isCopied` 保持 true 的时长（毫秒），默认 3000。
 * @returns `isCopied` 当前是否处于「已复制」高亮态，`copyToClipboard` 用于触发复制。
 */
export const useCopyToClipboard = ({
  copiedDuration = 3000,
}: UseCopyToClipboardOptions = {}) => {
  const [isCopied, setIsCopied] = useState<boolean>(false);
  const copiedTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (copiedTimeoutRef.current !== null) {
        clearTimeout(copiedTimeoutRef.current);
        copiedTimeoutRef.current = null;
      }
    };
  }, []);

  const copyToClipboard = (value: string) => {
    if (!value || typeof navigator === "undefined" || !navigator.clipboard) {
      return;
    }

    navigator.clipboard.writeText(value).then(
      () => {
        setIsCopied(true);
        if (copiedTimeoutRef.current !== null) {
          clearTimeout(copiedTimeoutRef.current);
        }
        copiedTimeoutRef.current = setTimeout(() => {
          copiedTimeoutRef.current = null;
          setIsCopied(false);
        }, copiedDuration);
      },
      (cause: unknown) => {
        void frontendLog(
          "WARNING",
          "clipboard_copy_failed",
          "复制到剪贴板失败",
          { data: { length: value.length }, error: cause },
        );
      },
    );
  };

  return { isCopied, copyToClipboard };
};
