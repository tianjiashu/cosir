"use client";

import { useMemo, useState, type PointerEvent } from "react";
import { BotIcon, GripVerticalIcon, PlusIcon, SearchIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { AgentConfiguration } from "@/lib/api/configuration";

export function AgentTeamGraphPalette({
  profiles,
  onAdd,
  onBeginDrag,
}: {
  profiles: AgentConfiguration[];
  onAdd: (profile: AgentConfiguration) => void;
  onBeginDrag: (profile: AgentConfiguration, event: PointerEvent<HTMLElement>) => void;
}) {
  const [query, setQuery] = useState("");
  const filteredProfiles = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    if (!normalizedQuery) return profiles;
    return profiles.filter((profile) => `${profile.agent_id} ${profile.role} ${profile.description}`.toLocaleLowerCase().includes(normalizedQuery));
  }, [profiles, query]);

  return (
    <aside className="bg-card flex w-64 shrink-0 flex-col border-r" aria-label="子 Agent 工具栏">
      <header className="border-b px-4 py-4">
        <div className="flex items-center gap-2"><BotIcon className="size-4 text-primary" /><h2 className="text-sm font-semibold">子 Agent</h2></div>
        <p className="text-muted-foreground mt-1 text-[11px]">拖到画布创建节点，也可以点 + 添加</p>
        <label className="relative mt-3 block"><SearchIcon className="text-muted-foreground absolute left-2.5 top-2.5 size-3.5" /><Input aria-label="搜索子 Agent" className="h-9 pl-8 text-xs" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索名称或用途" /></label>
      </header>
      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-3">
        {filteredProfiles.map((profile) => <article
          key={profile.agent_id}
          onPointerDown={(event) => onBeginDrag(profile, event)}
          className="border-border/70 bg-background group cursor-grab touch-none rounded-xl border p-3 shadow-sm transition hover:border-primary/40 hover:shadow-md active:cursor-grabbing"
          aria-label={`可拖拽子 Agent ${profile.agent_id}`}
        >
          <div className="flex items-start gap-2">
            <GripVerticalIcon className="text-muted-foreground mt-0.5 size-4 shrink-0 opacity-50 group-hover:opacity-100" />
            <div className="min-w-0 flex-1"><p className="truncate text-xs font-semibold">{profile.agent_id}</p><p className="text-muted-foreground mt-0.5 truncate text-[10px]">{profile.role}</p></div>
            <Button type="button" variant="ghost" size="icon-xs" aria-label={`添加 ${profile.agent_id}`} onClick={() => onAdd(profile)}><PlusIcon /></Button>
          </div>
          {profile.description && <p className="text-muted-foreground mt-2 line-clamp-2 text-[10px] leading-relaxed">{profile.description}</p>}
        </article>)}
        {filteredProfiles.length === 0 && <p className="text-muted-foreground rounded-xl border border-dashed p-4 text-center text-xs">{profiles.length ? "没有匹配的子 Agent" : "当前作用域没有可用的子 Agent"}</p>}
      </div>
      <footer className="text-muted-foreground border-t px-3 py-2 text-[10px]">可重复添加同一个 Agent</footer>
    </aside>
  );
}
