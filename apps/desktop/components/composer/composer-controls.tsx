"use client";

import type { ReactNode } from "react";

import { ModelSelector } from "@/components/assistant-ui/model-selector";

type ComposerControlsProps = {
  taskId?: number;
  workspacePicker?: ReactNode;
  trailing?: ReactNode;
  onModelReadyChange?: (ready: boolean) => void;
};

export function ComposerControls({ taskId, workspacePicker, trailing, onModelReadyChange }: ComposerControlsProps) {
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      {workspacePicker}
      <ModelSelector taskId={taskId} onReadyChange={onModelReadyChange} />
      {trailing}
    </div>
  );
}
