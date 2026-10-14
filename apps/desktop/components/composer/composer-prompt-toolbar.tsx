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
};

export function ComposerPromptToolbar({
  toolGroups,
  selectedToolGroups,
  onSelectedToolGroupsChange,
  toolGroupsLoading = false,
  toolGroupsError,
  disabled = false,
}: ComposerPromptToolbarProps) {
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      <FavoritePromptToolbar disabled={disabled} />
      <SkillsSelector disabled={disabled} />
      <ToolGroupSelector
        toolGroups={toolGroups}
        selectedToolGroups={selectedToolGroups}
        onSelectedToolGroupsChange={onSelectedToolGroupsChange}
        loading={toolGroupsLoading}
        error={toolGroupsError}
        disabled={disabled}
      />
    </div>
  );
}
