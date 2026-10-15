"use client";

import type { ReactNode } from "react";
import { RotateCwIcon, Trash2Icon, Undo2Icon } from "lucide-react";

import { ConfirmDialog } from "@/components/confirm/confirm-dialog";
import {
  acceptCurrentConfirmation,
  cancelCurrentConfirmation,
  useConfirmStore,
  type ConfirmationRequest,
} from "@/lib/confirm/confirm-store";

const ICONS: Record<NonNullable<ConfirmationRequest["icon"]>, ReactNode> = {
  delete: <Trash2Icon />,
  discard: <Undo2Icon />,
  restart: <RotateCwIcon />,
};

/** 全局确认框宿主：渲染 requestConfirmation() 队列中的第一项，应用根部只挂载一次。 */
export function ConfirmHost() {
  const current = useConfirmStore((state) => state.queue[0] ?? null);
  const busy = useConfirmStore((state) => state.busy);
  const error = useConfirmStore((state) => state.error);

  if (!current) return null;
  return (
    <ConfirmDialog
      key={current.id}
      open
      title={current.title}
      description={current.description}
      warning={current.warning}
      error={error}
      busy={busy}
      tone={current.tone}
      confirmLabel={current.confirmLabel}
      busyLabel={current.busyLabel ?? "处理中…"}
      confirmIcon={current.icon ? ICONS[current.icon] : undefined}
      onOpenChange={(open) => { if (!open) cancelCurrentConfirmation(); }}
      onConfirm={() => void acceptCurrentConfirmation()}
    />
  );
}
