"use client";

import type { ReactNode } from "react";

import { ModelSelector } from "@/components/assistant-ui/model-selector";
import type { ModelSelectionScope } from "@/lib/model-selection-storage";

type ComposerControlsProps = {
  scope?: ModelSelectionScope;
  runtimeModelContext?: boolean;
  workspacePicker?: ReactNode;
  trailing?: ReactNode;
  onModelReadyChange?: (ready: boolean) => void;
};

export function ComposerControls({ scope, runtimeModelContext = false, workspacePicker, trailing, onModelReadyChange }: ComposerControlsProps) {
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      {workspacePicker}
      <ModelSelector scope={scope} runtimeModelContext={runtimeModelContext} onReadyChange={onModelReadyChange} />
      {trailing}
    </div>
  );
}
