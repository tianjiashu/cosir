"use client";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";

/** 展示尚未被后端接受的 Assistant Transport 请求错误。 */
export function TransportErrorDialog({
  message,
  open,
  onOpenChange,
}: {
  message: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogTitle>发送失败</DialogTitle>
        <DialogDescription>{message}</DialogDescription>
      </DialogContent>
    </Dialog>
  );
}
