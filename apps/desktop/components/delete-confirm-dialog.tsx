"use client";

import { Trash2Icon } from "lucide-react";

import { ConfirmDialog, type ConfirmDialogProps } from "@/components/confirm-dialog";

type DeleteConfirmDialogProps = Omit<ConfirmDialogProps, "confirmLabel" | "busyLabel" | "confirmIcon">;

/** 删除类确认：固定「确认删除 / 删除中…」文案与图标。 */
export function DeleteConfirmDialog(props: DeleteConfirmDialogProps) {
  return (
    <ConfirmDialog
      {...props}
      confirmLabel="确认删除"
      busyLabel="删除中…"
      confirmIcon={<Trash2Icon />}
    />
  );
}
