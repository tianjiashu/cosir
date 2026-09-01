"use client";

import type { ReactNode } from "react";

import { ModelSelector } from "@/components/assistant-ui/model-selector";

type ComposerControlsProps = {
  taskId?: number;
  workspacePicker?: ReactNode;
  trailing?: ReactNode;
};

export function ComposerControls({ taskId, workspacePicker, trailing }: ComposerControlsProps) {
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      {workspacePicker}
      <ModelSelector taskId={taskId} />
      {trailing}
    </div>
  );
}
