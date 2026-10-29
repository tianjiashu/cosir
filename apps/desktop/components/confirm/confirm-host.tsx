"use client";

import type { ReactNode } from "react";
import { CircleAlertIcon, RotateCwIcon, Trash2Icon, Undo2Icon } from "lucide-react";

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
  error: <CircleAlertIcon />,
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
      title={current.title}
      description={current.description}
      irreversible={current.irreversible}
      warning={current.warning}
      error={error}
      busy={busy}
      tone={current.tone}
      notice={current.notice}
      confirmLabel={current.confirmLabel}
      busyLabel={current.busyLabel ?? "处理中…"}
      icon={current.icon ? ICONS[current.icon] : undefined}
      alternativeLabel={current.alternative?.label}
      // 告知类关闭（Esc、点遮罩）等同于按下唯一的按钮。
      onOpenChange={(open) => {
        if (open) return;
        if (current.notice) void acceptCurrentConfirmation("primary");
        else cancelCurrentConfirmation();
      }}
      onConfirm={() => void acceptCurrentConfirmation("primary")}
      onAlternative={() => void acceptCurrentConfirmation("alternative")}
    />
  );
}
