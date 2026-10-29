"use client";

import { FavoritePromptToolbar } from "@/components/composer/favorite-prompt-toolbar";
import { SkillsSelector } from "@/components/composer/skills-selector";
import { ToolGroupSelector } from "@/components/composer/tool-group-selector";
import type { ToolGroupCatalog } from "@/lib/api/tools";

type ComposerPromptToolbarProps = {
  toolGroups: ToolGroupCatalog[];
  selectedToolGroups: string[];
  onSelectedToolGroupsChange?: (groups: string[]) => void;
  toolGroupsLoading?: boolean;
  toolGroupsError?: string | null;
  disabled?: boolean;
  /** 工具组只读时的说明，见 {@link ToolGroupSelector} 的 `lockedHint`。 */
  toolGroupsLockedHint?: string;
  /** 为 false 时不显示工具组开关（如排队消息编辑：配置沿用发送时的值，界面无从回显）。 */
  showToolGroups?: boolean;
};

export function ComposerPromptToolbar({
  toolGroups,
  selectedToolGroups,
  onSelectedToolGroupsChange,
  toolGroupsLoading = false,
  toolGroupsError,
  disabled = false,
  toolGroupsLockedHint,
  showToolGroups = true,
}: ComposerPromptToolbarProps) {
  return (
    <div className="flex shrink-0 items-center gap-0.5">
      <FavoritePromptToolbar disabled={disabled} />
      <SkillsSelector disabled={disabled} />
      {showToolGroups && (
        <ToolGroupSelector
          toolGroups={toolGroups}
          selectedToolGroups={selectedToolGroups}
          onSelectedToolGroupsChange={onSelectedToolGroupsChange}
          loading={toolGroupsLoading}
          error={toolGroupsError}
          disabled={disabled}
          lockedHint={toolGroupsLockedHint}
        />
      )}
    </div>
  );
}
