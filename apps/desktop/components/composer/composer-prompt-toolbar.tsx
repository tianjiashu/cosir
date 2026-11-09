import { FavoritePromptToolbar } from "@/components/composer/favorite-prompt-toolbar";
import { SkillsSelector } from "@/components/composer/skills-selector";
import { ToolGroupSelector } from "@/components/composer/tool-group-selector";
import type { ToolGroupCatalog } from "@/lib/api/tool-groups";

type ComposerPromptToolbarProps = {
  toolGroups: ToolGroupCatalog[];
  selectedToolGroups: string[];
  onSelectedToolGroupsChange?: (groups: string[]) => void;
  toolGroupsLoading?: boolean;
  toolGroupsError?: string | null;
  disabled?: boolean;
  /** 工具组只读时的说明，见 {@link ToolGroupSelector} 的 `lockedHint`。 */
  toolGroupsLockedHint?: string;
  /** 当前工作区：Skill 选择器列出它看得到的 Skill；新对话还没选工作区时为 `null`，只列系统级。 */
  workspaceId: number | null;
};

export function ComposerPromptToolbar({
  toolGroups,
  selectedToolGroups,
  onSelectedToolGroupsChange,
  toolGroupsLoading = false,
  toolGroupsError,
  disabled = false,
  toolGroupsLockedHint,
  workspaceId,
}: ComposerPromptToolbarProps) {
  return (
    <div className="flex shrink-0 items-center gap-0.5">
      <FavoritePromptToolbar disabled={disabled} />
      <SkillsSelector disabled={disabled} workspaceId={workspaceId} />
      <ToolGroupSelector
        toolGroups={toolGroups}
        selectedToolGroups={selectedToolGroups}
        onSelectedToolGroupsChange={onSelectedToolGroupsChange}
        loading={toolGroupsLoading}
        error={toolGroupsError}
        disabled={disabled}
        lockedHint={toolGroupsLockedHint}
      />
    </div>
  );
}
