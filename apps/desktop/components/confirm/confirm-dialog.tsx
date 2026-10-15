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
  /** destructive：删除、中断等不可逆操作；default：普通确认。 */
  tone?: "destructive" | "default";
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
};

/**
 * 需要用户二次确认的操作对话框（纯呈现）。
 *
 * 业务代码优先使用 `requestConfirmation()`（lib/confirm/confirm-store.ts），由全局
 * ConfirmHost 渲染本组件并托管 busy / error；只有确认框需要嵌在特定布局中时才直接使用。
 * 不要再使用 `window.confirm`：它在 WebView 中会阻塞且无法呈现忙碌与失败状态。
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
  tone = "destructive",
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
          <Button type="button" variant={tone === "destructive" ? "destructive" : "default"} onClick={onConfirm} disabled={busy}>
            {busy ? <Spinner label={null} /> : confirmIcon}
            {busy ? busyLabel : confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
