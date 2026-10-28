"use client";

import { useMemo, useState, type PointerEvent } from "react";
import { CheckCircle2Icon, PlusIcon, SearchIcon } from "lucide-react";

import { AgentAvatar } from "@/components/agent-team-graph-nodes";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/ui/state-panel";
import type { AgentConfiguration } from "@/lib/api/configuration";

export function AgentTeamGraphPalette({
  profiles,
  onAdd,
  onBeginDrag,
  onBeginEndDrag,
}: {
  profiles: AgentConfiguration[];
  onAdd: (profile: AgentConfiguration) => void;
  onBeginDrag: (profile: AgentConfiguration, event: PointerEvent<HTMLElement>) => void;
  onBeginEndDrag: (event: PointerEvent<HTMLElement>) => void;
}) {
  const [query, setQuery] = useState("");
  const filteredProfiles = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    if (!normalizedQuery) return profiles;
    return profiles.filter((profile) => `${profile.agent_id} ${profile.role} ${profile.description}`.toLocaleLowerCase().includes(normalizedQuery));
  }, [profiles, query]);

  return (
    <aside className="bg-card flex w-60 shrink-0 flex-col border-r" aria-label="子 Agent 工具栏">
      <header className="space-y-3 px-3 pt-4 pb-3">
        <div className="px-1">
          <h2 className="text-sm font-semibold">子 Agent</h2>
          <p className="text-muted-foreground mt-0.5 text-2xs">拖到画布创建节点，或点 + 添加</p>
        </div>
        <label className="relative block"><SearchIcon className="text-muted-foreground absolute left-2.5 top-2.5 size-3.5" /><Input aria-label="搜索子 Agent" className="h-9 pl-8 text-xs" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索名称或用途" /></label>
      </header>
      <div className="min-h-0 flex-1 space-y-1 overflow-y-auto px-2 pb-3">
        {filteredProfiles.map((profile) => <article
          key={profile.agent_id}
          onPointerDown={(event) => onBeginDrag(profile, event)}
          className="hover:bg-muted/60 group cursor-grab touch-none rounded-xl p-2 transition-colors active:cursor-grabbing"
          aria-label={`可拖拽子 Agent ${profile.agent_id}`}
        >
          <div className="flex items-center gap-2.5">
            <AgentAvatar agentId={profile.agent_id} className="size-8" />
            <div className="min-w-0 flex-1"><p className="truncate text-xs font-semibold">{profile.agent_id}</p><p className="text-muted-foreground truncate text-2xs">{profile.role}</p></div>
            <Button type="button" variant="ghost" size="icon-xs" className="opacity-60 group-hover:opacity-100" aria-label={`添加 ${profile.agent_id}`} onClick={() => onAdd(profile)}><PlusIcon /></Button>
          </div>
          {/* 描述默认一行，悬停时完整展开，列表保持紧凑又不丢信息。 */}
          {profile.description && <p className="text-muted-foreground mt-1.5 line-clamp-1 pl-10.5 text-2xs leading-4 group-hover:line-clamp-none">{profile.description}</p>}
        </article>)}
        {filteredProfiles.length === 0 && <EmptyState size="compact" className="text-center" title={profiles.length ? "没有匹配的子 Agent" : "当前作用域没有可用的子 Agent"} />}
      </div>
      <div className="border-t p-2">
        <article
          onPointerDown={onBeginEndDrag}
          className="border-success/30 text-success hover:bg-success/10 flex cursor-grab touch-none items-center gap-2.5 rounded-xl border border-dashed p-2 transition-colors active:cursor-grabbing"
          aria-label="可拖拽结束出口 END"
        >
          <span className="bg-success/10 flex size-8 shrink-0 items-center justify-center rounded-xl"><CheckCircle2Icon className="size-4" /></span>
          <div className="min-w-0"><p className="text-xs font-semibold">END</p><p className="text-2xs opacity-80">拖入画布添加结束出口</p></div>
        </article>
      </div>
    </aside>
  );
}
