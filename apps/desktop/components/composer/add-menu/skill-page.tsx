import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { PuzzleIcon } from "lucide-react";

import { Command, CommandEmpty, CommandInput, CommandItem, CommandList } from "@/components/ui/command";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import { insertCapsules, useComposerInsertion } from "@/components/composer/composer-insertion-context";
import { type Skill } from "@/lib/api/extensions";
import { skillsQueryOptions } from "@/lib/query/query-options";
import { AddMenuPageHeader } from "./add-menu-page-header";

const EMPTY_SKILLS: Skill[] = [];

/**
 * 「+」菜单的 Skill 页：列出当前工作区可用的 Skill（系统级与工作区合并，同名时工作区优先，只列已启用的），
 * 选中后在光标处插入 Skill 胶囊。
 *
 * 只在这一页打开时挂载，所以列表随打开才请求；与配置中心的 Skill 列表共用查询缓存，配置侧写入后失效对应
 * 作用域（系统级写入失效全部作用域），这里不自行刷新。列表用 cmdk：输入即筛选（名称与描述都参与匹配），
 * 方向键选择、回车插入。
 */
export function SkillPage({ workspaceId, onBack, onPicked }: {
  /** `null` 时只列系统级（新对话页还没选工作区）。 */
  workspaceId: number | null;
  onBack: () => void;
  onPicked: () => void;
}) {
  const insertion = useComposerInsertion();
  const skillsQuery = useQuery(skillsQueryOptions(workspaceId));
  const skills = skillsQuery.data ?? EMPTY_SKILLS;
  const loading = skillsQuery.isFetching && !skillsQuery.data;
  const failed = skillsQuery.isError && !skillsQuery.isFetching;
  const enabledSkills = useMemo(
    () => skills.filter((skill) => skill.enabled).sort((left, right) => left.name.localeCompare(right.name)),
    [skills],
  );

  return (
    <>
      <AddMenuPageHeader title="Skill" onBack={onBack} />
      {loading ? (
        <LoadingState size="compact" label="正在加载 Skills…" className="px-3" />
      ) : failed ? (
        <ErrorState className="m-2" message="Skills 加载失败" onRetry={() => void skillsQuery.refetch()} />
      ) : (
        <Command>
          <CommandInput placeholder="搜索 Skill" aria-label="搜索 Skill" autoFocus />
          <CommandList aria-label="可用 Skills" className="max-h-64">
            <CommandEmpty className="text-muted-foreground py-4 text-xs">
              {enabledSkills.length > 0 ? "没有匹配的 Skill" : "没有已启用的 Skill"}
            </CommandEmpty>
            {enabledSkills.map((skill) => (
              <CommandItem
                key={skill.name}
                value={skill.name}
                keywords={skill.description ? [skill.description] : undefined}
                onSelect={() => {
                  insertCapsules(insertion.dispatch, [{ kind: "skill", name: skill.name }]);
                  onPicked();
                }}
                className="flex-col items-start gap-0.5 rounded-lg px-2.5 py-2 [&>svg:last-child]:hidden"
              >
                <span className="flex items-center gap-1.5 text-sm font-medium">
                  <PuzzleIcon className="text-muted-foreground size-3.5" />{skill.name}
                </span>
                {skill.description && <span className="text-muted-foreground line-clamp-2 pl-5 text-xs">{skill.description}</span>}
              </CommandItem>
            ))}
          </CommandList>
        </Command>
      )}
    </>
  );
}
