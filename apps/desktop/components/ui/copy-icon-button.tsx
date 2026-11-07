import { CheckIcon, CopyIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { Button } from "@/components/ui/button";
import type { ComponentProps } from "react";
import { frontendLog } from "@/lib/logging/frontend-log";

const COPIED_RESET_MS = 1_500;

export type CopyIconButtonProps = Omit<ComponentProps<typeof Button>, "onClick" | "children"> & {
  /** 按钮的说明（tooltip 与无障碍名称），如「复制命令」。 */
  label: string;
  /** 工具输出使用明确文字；消息操作栏保留图标与 tooltip。 */
  showLabel?: boolean;
  /** 点击时才取文本：调用方的文本可能来自渲染状态（如终端缓冲），不必每次渲染都算。 */
  getText: () => string;
  /** 写剪贴板失败时的日志事件与字段。 */
  logEvent: string;
  logData?: Record<string, unknown>;
};

/**
 * 就地复制的图标按钮：成功后图标变对勾一会儿，不弹 toast（反馈就在按钮上）；失败就地提示并记 WARNING 日志。
 * 取到空文本时什么都不做，不清空用户的剪贴板。需要全应用一致的成功 / 失败提示时用 `lib/clipboard.ts` 的
 * `copyText`。
 */
export function CopyIconButton({ label, getText, logEvent, logData, showLabel = false, ...rest }: CopyIconButtonProps) {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), COPIED_RESET_MS);
    return () => window.clearTimeout(timer);
  }, [copied]);

  const copy = async () => {
    setFailed(false);
    try {
      const text = getText();
      if (!text) return;
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch (error) {
      setFailed(true);
      void frontendLog("WARNING", logEvent, "写入剪贴板失败", { data: logData, error });
    }
  };

  const feedback = failed ? "复制失败，请重试" : copied ? "已复制" : label;
  const icon = copied ? <CheckIcon /> : <CopyIcon />;
  return showLabel ? (
    <Button variant="ghost" size="sm" aria-label={label} {...rest} onClick={() => void copy()}>
      {icon}<span aria-live="polite">{feedback}</span>
    </Button>
  ) : (
    <TooltipIconButton tooltip={feedback} aria-label={label} {...rest} onClick={() => void copy()}>{icon}</TooltipIconButton>
  );
}
