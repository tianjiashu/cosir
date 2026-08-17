/**
 * 剪贴板复制 Hook（内部复用，替代散落的同构样板）。
 *
 * 统一「写入剪贴板 → 短暂 `copied` 反馈 → 2 秒后复位」行为，
 * 消除 CodeBlock / TerminalCallCard / StatusBadge 中重复的
 * `navigator.clipboard.writeText` + `useState(copied)` + `setTimeout(reset, 2000)` 样板。
 * 不引入外部库（`use-copy-to-clipboard` 已停更且依赖 react@16，排除）。
 *
 * @module hooks/useCopyToClipboard
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { logWarn } from "@/lib/logger";

/** 复制成功反馈的展示时长（毫秒）。 */
const COPIED_RESET_MS = 2000;

/**
 * 提供「复制到剪贴板并短暂标记已复制」能力的 Hook。
 *
 * 目的：
 *   封装剪贴板写入与 `copied` 状态机的公共逻辑，调用方无需各自维护
 *   `useState` + `setTimeout` 样板。复制失败经统一日志出口 `logWarn` 记录
 *   （携带 `module` 上下文，不静默吞错，便于排查环境剪贴板权限问题）。
 *
 * 参数：
 *   无。
 *
 * 返回：
 *   - `copied`：是否已复制成功（2 秒内为 true）。
 *   - `copy`：执行复制的回调，接收待复制文本，返回复制是否成功的 Promise<boolean>。
 *
 * 异常：
 *   复制失败时捕获异常并经 `logWarn` 记录，不向上抛出；`copy` 返回 false。
 *
 * 副作用：
 *   调用 `navigator.clipboard.writeText` 写入系统剪贴板；
 *   在复制成功后启动定时器，2 秒后将 `copied` 复位为 false；
 *   组件卸载时清理未触发的复位定时器，避免对已卸载组件 setState。
 */
export function useCopyToClipboard(): {
  copied: boolean;
  copy: (text: string) => Promise<boolean>;
} {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 卸载时清理定时器，避免对未挂载组件触发 setState（React 告警）。
  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  const copy = useCallback(async (text: string): Promise<boolean> => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => setCopied(false), COPIED_RESET_MS);
      return true;
    } catch (err) {
      // 复制失败经统一日志出口记录（带 module 上下文），不静默吞错；返回 false。
      logWarn("复制到剪贴板失败", {
        module: "useCopyToClipboard",
        error: err instanceof Error ? err.message : String(err),
      });
      return false;
    }
  }, []);

  return { copied, copy };
}
