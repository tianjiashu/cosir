"use client";

import { useRef, type ReactNode } from "react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogTitle,
} from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import type { ConfirmChoice, ConfirmTone } from "@/lib/confirm/confirm-store";
import { cn } from "@/lib/utils";

export type ConfirmDialogProps = {
  title: string;
  description: string;
  /** 在说明下方加一行「此操作无法撤销。」。 */
  irreversible?: boolean;
  /** 需要醒目强调的后果（会中断运行等）。 */
  warning?: string;
  error?: string | null;
  /** 正在执行的选择：禁用按钮并阻止关闭，避免重复提交或中途丢失结果；对应按钮显示忙碌态。 */
  busy?: ConfirmChoice | null;
  confirmLabel: string;
  busyLabel: string;
  /** 标题左侧图标；配色由 `tone` 决定。 */
  icon?: ReactNode;
  tone?: ConfirmTone;
  /** 与主操作并列的另一种确认，渲染在主按钮左侧的描边按钮。 */
  alternativeLabel?: string;
  /** 只告知、不需要选择：只渲染主按钮，没有取消。 */
  notice?: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
  onAlternative?: () => void;
};

const ICON_BADGE_CLASS: Record<ConfirmTone, string> = {
  destructive: "bg-destructive/10 text-destructive",
  warning: "bg-warning/10 text-warning",
  default: "bg-muted text-muted-foreground",
};

/**
 * 确认与失败告知的统一对话框（纯呈现，挂载即打开，由调用方决定何时挂载）。
 *
 * 业务代码使用 `requestConfirmation()` / `confirmDelete()` / `confirmDiscard()` /
 * `showErrorDialog()`（lib/confirm/confirm-store.ts），由全局 ConfirmHost 渲染本组件并托管
 * busy / error。不要再使用 `window.confirm`：它在 WebView 中会阻塞且无法呈现忙碌与失败状态。
 *
 * 打开时焦点落在「取消」：回车不会直接执行删除这类操作。告知类没有取消，焦点落在唯一的按钮上。
 */
export function ConfirmDialog({
  title,
  description,
  irreversible = false,
  warning,
  error,
  busy = null,
  confirmLabel,
  busyLabel,
  icon,
  tone = "default",
  alternativeLabel,
  notice = false,
  onOpenChange,
  onConfirm,
  onAlternative,
}: ConfirmDialogProps) {
  const cancelRef = useRef<HTMLButtonElement | null>(null);
  const isBusy = busy !== null;
  return (
    <Dialog open onOpenChange={(nextOpen) => { if (!isBusy) onOpenChange(nextOpen); }}>
      <DialogContent showCloseButton={false} initialFocus={notice ? true : cancelRef}>
        <div className="flex gap-3">
          {icon && (
            <span
              aria-hidden="true"
              className={cn("flex size-9 shrink-0 items-center justify-center rounded-full [&_svg]:size-4", ICON_BADGE_CLASS[tone])}
            >
              {icon}
            </span>
          )}
          <div className="flex min-w-0 flex-1 flex-col gap-1.5 pt-0.5">
            <DialogTitle className="leading-snug">{title}</DialogTitle>
            {description && (
              <DialogDescription className="whitespace-pre-wrap break-words">{description}</DialogDescription>
            )}
            {irreversible && <p className="text-destructive text-xs">此操作无法撤销。</p>}
          </div>
        </div>
        {warning && <Alert tone="warning" role="note">{warning}</Alert>}
        {error && <Alert tone="destructive">{error}</Alert>}
        <DialogFooter>
          {!notice && (
            <Button ref={cancelRef} type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={isBusy}>取消</Button>
          )}
          {alternativeLabel && onAlternative && (
            <Button type="button" variant="outline" onClick={onAlternative} disabled={isBusy}>
              {busy === "alternative" && <Spinner label={null} />}
              {busy === "alternative" ? busyLabel : alternativeLabel}
            </Button>
          )}
          <Button
            type="button"
            variant={tone === "destructive" && !notice ? "destructive" : "default"}
            onClick={onConfirm}
            disabled={isBusy}
          >
            {busy === "primary" && <Spinner label={null} />}
            {busy === "primary" ? busyLabel : confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
