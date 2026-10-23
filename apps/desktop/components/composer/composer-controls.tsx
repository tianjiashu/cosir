"use client";

import { ModelSelector } from "@/components/assistant-ui/model-selector";
import type { TransportRunLaunch } from "@/lib/assistant/contract";
import type { ModelSelectionScope } from "@/lib/model-selection-storage";

type ComposerControlsProps = {
  scope?: ModelSelectionScope;
  runtimeModelContext?: boolean;
  onModelReadyChange?: (ready: boolean) => void;
  /** 运行期配置锁定的 Run 配置，见 `ModelSelector` 的 `locked`。 */
  locked?: TransportRunLaunch | null;
};

export function ComposerControls({ scope, runtimeModelContext = false, onModelReadyChange, locked }: ComposerControlsProps) {
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      <ModelSelector scope={scope} runtimeModelContext={runtimeModelContext} onReadyChange={onModelReadyChange} locked={locked} />
    </div>
  );
}
