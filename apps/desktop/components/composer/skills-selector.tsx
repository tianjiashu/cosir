"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import { HammerIcon, LoaderCircleIcon, SearchIcon } from "lucide-react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Input } from "@/components/ui/input";
import { getSystemSkills, type SystemSkill } from "@/lib/api/system-extensions";
import { insertSkill, useComposerInsertion } from "@/components/composer/composer-insertion-context";

type SkillsSelectorProps = {
  disabled?: boolean;
};

/** 选择当前可用的系统 Skill，并把 mention 插入最近的 composer 光标位置。 */
export function SkillsSelector({ disabled = false }: SkillsSelectorProps) {
  const insertion = useComposerInsertion();
  const [open, setOpen] = useState(false);
  const [skills, setSkills] = useState<SystemSkill[]>([]);
  const [loading, setLoading] = useState(false);
  const loadingRef = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  const loadSkills = useCallback(async () => {
    if (loadingRef.current) return;
    loadingRef.current = true;
    setLoading(true);
    setError(null);
    try {
      setSkills(await getSystemSkills());
    } catch {
      setError("Skills 加载失败，请重试");
    } finally {
      loadingRef.current = false;
      setLoading(false);
    }
  }, []);

  const enabledSkills = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    return skills
      .filter((skill) => skill.enabled)
      .filter((skill) => !normalizedQuery
        || skill.name.toLocaleLowerCase().includes(normalizedQuery)
        || skill.description.toLocaleLowerCase().includes(normalizedQuery))
      .sort((left, right) => left.name.localeCompare(right.name));
  }, [query, skills]);

  return (
    <Popover open={open} onOpenChange={(nextOpen) => {
      setOpen(nextOpen);
      if (!nextOpen) setQuery("");
      if (nextOpen) void loadSkills();
    }}>
      <PopoverTrigger
        type="button"
        disabled={disabled}
        className="border-border bg-background hover:bg-muted inline-flex h-7 items-center gap-1.5 rounded-full border px-2.5 text-xs font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50"
        aria-label="选择 Skill"
      >
        <HammerIcon className="size-3.5" />
        <span>Skills</span>
      </PopoverTrigger>
      <PopoverContent align="start" side="top" sideOffset={6} className="w-80 rounded-xl p-2 shadow-lg">
        <div className="relative">
          <SearchIcon className="text-muted-foreground pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2" />
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索 Skill"
            aria-label="搜索 Skill"
            className="h-8 pl-8 text-xs"
          />
        </div>
        {loading && (
          <p className="text-muted-foreground flex items-center gap-2 px-2 py-4 text-xs" role="status">
            <LoaderCircleIcon className="size-3.5 animate-spin" />正在加载 Skills…
          </p>
        )}
        {!loading && error && (
          <div className="flex items-center justify-between gap-2 px-2 py-3">
            <p className="text-destructive text-xs" role="alert">{error}</p>
            <button type="button" onClick={() => void loadSkills()} className="text-xs font-medium underline underline-offset-2">重试</button>
          </div>
        )}
        {!loading && !error && enabledSkills.length === 0 && (
          <p className="text-muted-foreground px-2 py-4 text-xs">
            {skills.some((skill) => skill.enabled) ? "没有匹配的 Skill" : "没有已启用的 Skill"}
          </p>
        )}
        {!loading && !error && enabledSkills.length > 0 && (
          <div role="listbox" aria-label="可用 Skills" className="mt-1 max-h-64 space-y-0.5 overflow-y-auto">
            {enabledSkills.map((skill) => (
              <button
                key={skill.name}
                type="button"
                role="option"
                aria-selected="false"
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => {
                  insertSkill(insertion.dispatch, { name: skill.name });
                  setOpen(false);
                }}
                className="hover:bg-muted focus-visible:bg-muted flex w-full flex-col items-start gap-0.5 rounded-lg px-2.5 py-2 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
              >
                <span className="flex items-center gap-1.5 text-sm font-medium">
                  <HammerIcon className="text-muted-foreground size-3.5" />{skill.name}
                </span>
                {skill.description && <span className="text-muted-foreground line-clamp-2 pl-5 text-xs">{skill.description}</span>}
              </button>
            ))}
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}
