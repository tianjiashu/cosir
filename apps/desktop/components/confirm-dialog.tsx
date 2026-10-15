"use client";

import type { ReactNode } from "react";

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

export type ConfirmDialogProps = {
  open: boolean;
  title: string;
  description: string;
  /** 操作后果的醒目提示（不可恢复、会中断运行等）。 */
  warning?: string;
  error?: string | null;
  /** 确认操作进行中：禁用按钮并阻止关闭，避免重复提交或中途丢失结果。 */
  busy?: boolean;
  confirmLabel: string;
  busyLabel: string;
  confirmIcon?: ReactNode;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
};

/**
 * 需要用户二次确认的破坏性 / 中断性操作对话框。
 *
 * 只负责呈现与按钮状态；操作本身、busy 与 error 由调用方持有。
 * 删除、重启后端等确认都应复用它，而不是使用 `window.confirm` 或各写一个对话框。
 */
export function ConfirmDialog({
  open,
  title,
  description,
  warning,
  error,
  busy = false,
  confirmLabel,
  busyLabel,
  confirmIcon,
  onOpenChange,
  onConfirm,
}: ConfirmDialogProps) {
  return (
    <Dialog open={open} onOpenChange={(nextOpen) => { if (!busy) onOpenChange(nextOpen); }}>
      <DialogContent>
        <DialogTitle>{title}</DialogTitle>
        <DialogDescription>{description}</DialogDescription>
        {warning && <Alert tone="warning" role="note">{warning}</Alert>}
        {error && <Alert tone="destructive">{error}</Alert>}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={busy}>取消</Button>
          <Button type="button" variant="destructive" onClick={onConfirm} disabled={busy}>
            {busy ? <Spinner label={null} /> : confirmIcon}
            {busy ? busyLabel : confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
