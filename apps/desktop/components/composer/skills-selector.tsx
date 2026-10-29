"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { HammerIcon } from "lucide-react";

import { Command, CommandEmpty, CommandInput, CommandItem, CommandList } from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import { getSystemSkills, type SystemSkill } from "@/lib/api/system-extensions";
import { insertSkill, useComposerInsertion } from "@/components/composer/composer-insertion-context";
import { COMPOSER_ICON_BUTTON_CLASS_NAME } from "@/components/composer/composer-surface";
import { queryKeys } from "@/lib/query/query-keys";

const EMPTY_SKILLS: SystemSkill[] = [];

type SkillsSelectorProps = {
  disabled?: boolean;
};

/**
 * 选择当前可用的系统 Skill，并把 mention 插入最近的 composer 光标位置。
 *
 * 列表用 cmdk：方向键选择、回车插入、输入即筛选（名称与描述都参与匹配，按相关度排序）。
 */
export function SkillsSelector({ disabled = false }: SkillsSelectorProps) {
  const insertion = useComposerInsertion();
  const [open, setOpen] = useState(false);
  // 与配置中心的 Skill 列表共用缓存：启用 / 禁用后由配置侧失效 skills()，这里无需自行刷新。
  const skillsQuery = useQuery({
    queryKey: queryKeys.skills(),
    queryFn: ({ signal }) => getSystemSkills({ signal }),
    enabled: open,
  });
  const skills = skillsQuery.data ?? EMPTY_SKILLS;
  const loading = skillsQuery.isFetching && !skillsQuery.data;
  const failed = skillsQuery.isError && !skillsQuery.isFetching;
  const enabledSkills = useMemo(
    () => skills.filter((skill) => skill.enabled).sort((left, right) => left.name.localeCompare(right.name)),
    [skills],
  );

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        type="button"
        disabled={disabled}
        className={COMPOSER_ICON_BUTTON_CLASS_NAME}
        aria-label="选择 Skill"
        title="Skills"
      >
        <HammerIcon />
      </PopoverTrigger>
      <PopoverContent align="start" side="top" sideOffset={6} className="w-80 rounded-xl p-0 shadow-lg">
        {loading ? (
          <LoadingState size="compact" label="正在加载 Skills…" className="px-3" />
        ) : failed ? (
          <ErrorState className="m-2" message="Skills 加载失败" onRetry={() => void skillsQuery.refetch()} />
        ) : (
          // 关闭弹层即卸载 Command，下次打开时搜索词自然清空。
          <Command>
            <CommandInput placeholder="搜索 Skill" aria-label="搜索 Skill" />
            <CommandList aria-label="可用 Skills" className="max-h-64">
              <CommandEmpty className="text-muted-foreground py-4 text-xs">
                {enabledSkills.length > 0 ? "没有匹配的 Skill" : "没有已启用的 Skill"}
              </CommandEmpty>
              {enabledSkills.map((skill) => (
                <CommandItem
                  key={skill.name}
                  value={skill.name}
                  keywords={skill.description ? [skill.description] : undefined}
                  // 不抢走 composer 的选区，插入位置仍是用户最后的光标处。
                  onMouseDown={(event) => event.preventDefault()}
                  onSelect={() => {
                    insertSkill(insertion.dispatch, { name: skill.name });
                    setOpen(false);
                  }}
                  className="flex-col items-start gap-0.5 rounded-lg px-2.5 py-2 [&>svg:last-child]:hidden"
                >
                  <span className="flex items-center gap-1.5 text-sm font-medium">
                    <HammerIcon className="text-muted-foreground size-3.5" />{skill.name}
                  </span>
                  {skill.description && <span className="text-muted-foreground line-clamp-2 pl-5 text-xs">{skill.description}</span>}
                </CommandItem>
              ))}
            </CommandList>
          </Command>
        )}
      </PopoverContent>
    </Popover>
  );
}
