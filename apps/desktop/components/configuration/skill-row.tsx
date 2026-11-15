import { Link } from "react-router-dom";

import { SkillBadges, SkillWarnings } from "@/components/configuration/skill-badges";
import { skillDetailPath } from "@/components/configuration/use-skill-actions";
import { Switch } from "@/components/ui/switch";
import type { ExtensionScopeId, SkillEntry } from "@/lib/api/extensions";
import { cn } from "@/lib/utils";

/**
 * Skills 页的一行：名称 · 一行描述 · 特征徽标 · 开关或处境说明。
 *
 * 只有生效的条目能进详情、能启停：详情与停用名单都按名称寻址，对被覆盖的条目操作，落到的会是生效的
 * 同名 Skill。`editable` 由分组决定（继承来的系统级分组只读）。
 */
export function SkillRow({ entry, workspaceId, editable, busy, shadowedBy, onToggle }: {
  entry: SkillEntry;
  workspaceId: ExtensionScopeId;
  editable: boolean;
  busy: boolean;
  /** 被覆盖时，生效的那条所在目录的称呼。 */
  shadowedBy?: string;
  onToggle: () => void;
}) {
  const effective = entry.standing === "effective";
  return (
    <li className={cn("flex items-center gap-3 px-4 py-2", !effective && "text-muted-foreground")}>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-2">
          {effective ? (
            <Link to={skillDetailPath(workspaceId, entry.name)} className="focus-ring shrink-0 truncate rounded-sm text-sm font-medium hover:underline">{entry.name}</Link>
          ) : (
            <span className="shrink-0 truncate text-sm">{entry.name}</span>
          )}
          <span className="text-muted-foreground min-w-0 flex-1 truncate text-xs" title={entry.description}>{entry.description}</span>
          <SkillBadges skill={entry} />
        </div>
        <SkillWarnings warnings={entry.warnings} />
      </div>
      <div className="flex shrink-0 justify-end text-xs">
        {entry.standing === "shadowed" ? (
          // 详情按名称落到生效的那条，所以链接直接通向覆盖它的 Skill。
          <Link to={skillDetailPath(workspaceId, entry.name)} className="focus-ring rounded-sm hover:underline">被「{shadowedBy}」的同名 Skill 覆盖 →</Link>
        ) : editable ? (
          <Switch checked={entry.enabled} aria-label={`${entry.enabled ? "停用" : "启用"} Skill ${entry.name}`} disabled={busy} onClick={onToggle} />
        ) : (
          <span className="text-muted-foreground">{entry.enabled ? "已启用" : "已停用"}</span>
        )}
      </div>
    </li>
  );
}
