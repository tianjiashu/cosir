import { ConfigListSection } from "@/components/configuration/config-list-section";
import { SkillRow } from "@/components/configuration/skills/skill-row";
import { scopeOf } from "@/lib/api/configuration-scope";
import type { SkillEntry, SkillGroup } from "@/lib/api/extensions";
import { SKILL_ROOT_COPY, skillRootLabel } from "@/lib/skills/skill-directory";

/**
 * 一个 Skill 根目录（作用域 × 身份）的一节：标题行由 `ConfigListSection` 给出（称呼、数量、谁能看到、目录位置），
 * 下面是没有分隔线的列表。本层的空目录照样列出，按身份告诉用户往哪放；搜索时只传入命中的条目（没命中的分组整组不渲染）。
 * 继承来的系统级一节只读。
 */
export function SkillRootGroup({ group, skills, workspaceId, busy, effectiveLabels, onToggleSkill }: {
  group: SkillGroup;
  /** 本组要显示的条目（搜索后）。 */
  skills: SkillEntry[];
  workspaceId: number | null;
  busy: boolean;
  /** 每个名称生效的那条所在目录的称呼，给被覆盖的行用。 */
  effectiveLabels: Map<string, string>;
  onToggleSkill: (entry: SkillEntry) => void;
}) {
  const copy = SKILL_ROOT_COPY[group.kind];
  const pageScope = scopeOf(workspaceId);
  // 只有页面自己这一层可编辑，继承来的系统级分组只读。
  const editable = group.scope === pageScope;

  return (
    <ConfigListSection
      title={skillRootLabel(group.scope, group.kind, pageScope)}
      count={group.skills.length}
      info={<>{copy.audience}{group.kind === "cosir" && editable && "；「导入」的 Skill 放在这里"}</>}
      readOnly={!editable}
      location={group.location}
    >
      {group.skipped.length > 0 && (
        <details className="px-2 text-xs">
          <summary className="text-warning cursor-pointer select-none">{group.skipped.length} 个无法加载</summary>
          <ul className="text-muted-foreground mt-1 space-y-0.5">
            {group.skipped.map((item) => (
              <li key={item.directory}><code>{item.directory}</code>：{item.reason}</li>
            ))}
          </ul>
        </details>
      )}
      {skills.length > 0 ? (
        <ul>
          {skills.map((entry) => (
            <SkillRow
              key={entry.name}
              entry={entry}
              workspaceId={workspaceId}
              editable={editable}
              busy={busy}
              shadowedBy={entry.standing === "shadowed" ? effectiveLabels.get(entry.name) : undefined}
              onToggle={() => onToggleSkill(entry)}
            />
          ))}
        </ul>
      ) : (
        <p className="text-muted-foreground px-2 py-1.5 text-xs">
          {group.kind === "cosir"
            ? "还没有。用右上角「导入」放进来，只有 cosir 使用。"
            : "还没有。用 npx skills 或 Codex 等工具装到这里，装好后点「重新扫描」。"}
        </p>
      )}
    </ConfigListSection>
  );
}
