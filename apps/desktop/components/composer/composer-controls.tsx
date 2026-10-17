"use client";

import { ModelSelector } from "@/components/assistant-ui/model-selector";
import type { ModelSelectionScope } from "@/lib/model-selection-storage";

type ComposerControlsProps = {
  scope?: ModelSelectionScope;
  runtimeModelContext?: boolean;
  onModelReadyChange?: (ready: boolean) => void;
};

export function ComposerControls({ scope, runtimeModelContext = false, onModelReadyChange }: ComposerControlsProps) {
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      <ModelSelector scope={scope} runtimeModelContext={runtimeModelContext} onReadyChange={onModelReadyChange} />
    </div>
  );
}
