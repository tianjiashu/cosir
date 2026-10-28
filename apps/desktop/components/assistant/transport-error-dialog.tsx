"use client";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";

/** 展示尚未被后端接受的 Assistant Transport 请求错误；挂载即打开，由调用方决定何时挂载。 */
export function TransportErrorDialog({
  message,
  onOpenChange,
}: {
  message: string;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogTitle>发送失败</DialogTitle>
        <DialogDescription>{message}</DialogDescription>
      </DialogContent>
    </Dialog>
  );
}
