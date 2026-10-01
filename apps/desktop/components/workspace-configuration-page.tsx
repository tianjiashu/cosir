"use client";

import { useEffect, useMemo, useState } from "react";
import { ChevronLeftIcon, Code2Icon, FileTextIcon, FilterIcon, SaveIcon, Settings2Icon } from "lucide-react";

import { MarkdownConfigurationPanel } from "@/components/configuration/markdown-configuration-panel";
import { AgentConfigurationPanel, type AgentConfigurationApi } from "@/components/system-configuration-page";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  createWorkspaceAgentConfiguration,
  deleteWorkspaceAgentConfiguration,
  getWorkspaceAgentConfigurations,
  getWorkspaceFileIgnoreConfiguration,
  getWorkspaceInstructionConfiguration,
  updateWorkspaceAgentConfiguration,
  updateWorkspaceFileIgnoreConfiguration,
  updateWorkspaceInstructionConfiguration,
  type AgentConfigurationInput,
  type WorkspaceFileIgnoreConfiguration,
} from "@/lib/api/configuration";

function ErrorNotice({ message }: { message: string | null }) {
  if (!message) return null;
  return <div role="alert" className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">{message}</div>;
}

function WorkspaceFileIgnorePanel({ workspaceId }: { workspaceId: number }) {
  const [document, setDocument] = useState<WorkspaceFileIgnoreConfiguration | null>(null);
  const [content, setContent] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);

  const load = async () => {
    try {
      setError(null);
      const value = await getWorkspaceFileIgnoreConfiguration(workspaceId);
      setDocument(value);
      setContent(value.content);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取 .fileignore 失败");
    }
  };
  useEffect(() => { void load(); }, [workspaceId]);

  const save = async () => {
    setSaving(true);
    setSaved(false);
    setError(null);
    try {
      const next = await updateWorkspaceFileIgnoreConfiguration(workspaceId, content);
      setDocument(next);
      setContent(next.content);
      setSaved(true);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存 .fileignore 失败");
    } finally {
      setSaving(false);
    }
  };

  if (!document) return <div className="text-muted-foreground py-12 text-sm">{error ?? "正在读取 .fileignore…"}</div>;
  return <div className="space-y-4"><ErrorNotice message={error} /><section className="border-border/70 bg-card/70 rounded-2xl border p-4 shadow-sm"><p className="text-muted-foreground mb-3 text-xs">文件：.cosir/.fileignore。使用标准 gitignore 语法，搜索时立即生效。</p><Textarea className="min-h-[28rem] resize-y border-0 bg-transparent p-2 font-mono text-sm shadow-none focus-visible:ring-0" value={content} onChange={(event) => { setContent(event.target.value); setSaved(false); }} /><div className="mt-4 flex items-center justify-end gap-2 border-t pt-4">{saved && <span className="text-muted-foreground text-xs">已保存</span>}<Button onClick={() => void save()} disabled={saving}>{saving ? "保存中…" : <><SaveIcon />保存 .fileignore</>}</Button></div></section></div>;
}

export function WorkspaceConfigurationPage({ workspaceId, workspaceName, onClose }: { workspaceId: number; workspaceName: string; onClose: () => void }) {
  const [tab, setTab] = useState<"agents" | "instructions" | "fileignore">("agents");
  const agentApi = useMemo<AgentConfigurationApi>(() => ({
    list: () => getWorkspaceAgentConfigurations(workspaceId),
    create: (input: AgentConfigurationInput) => createWorkspaceAgentConfiguration(workspaceId, input),
    update: (agentId: string, input: AgentConfigurationInput) => updateWorkspaceAgentConfiguration(workspaceId, agentId, input),
    remove: (agentId: string) => deleteWorkspaceAgentConfiguration(workspaceId, agentId),
  }), [workspaceId]);
  const instructionAdapter = useMemo(() => ({
    load: () => getWorkspaceInstructionConfiguration(workspaceId),
    save: (content: string) => updateWorkspaceInstructionConfiguration(workspaceId, content),
    allowEmpty: true,
  }), [workspaceId]);

  const tabs = [
    { id: "agents" as const, label: "子 Agent", icon: Code2Icon },
    { id: "instructions" as const, label: "AGENTS.md", icon: FileTextIcon },
    { id: "fileignore" as const, label: ".fileignore", icon: FilterIcon },
  ];
  return <div className="bg-background/95 absolute inset-0 z-30 overflow-y-auto backdrop-blur-sm"><div className="mx-auto flex min-h-full w-full max-w-6xl flex-col px-5 py-6 lg:px-10"><header className="mb-8 flex flex-wrap items-start justify-between gap-4"><div><div className="text-muted-foreground mb-3 flex items-center gap-2 text-xs"><Settings2Icon className="size-3.5" />工作区配置 · {workspaceName}</div><h1 className="text-2xl font-semibold tracking-tight">配置当前 workspace</h1></div><Button variant="outline" onClick={onClose}><ChevronLeftIcon />返回工作区</Button></header><div className="grid min-h-0 flex-1 gap-8 lg:grid-cols-[13rem_minmax(0,1fr)]"><nav className="flex gap-2 overflow-x-auto lg:block lg:space-y-2" aria-label="工作区配置分类">{tabs.map(({ id, label, icon: Icon }) => <button key={id} type="button" onClick={() => setTab(id)} className={`flex min-w-44 items-center gap-3 rounded-xl px-3 py-3 text-left transition-colors lg:w-full ${tab === id ? "bg-primary text-primary-foreground shadow-sm" : "text-muted-foreground hover:bg-muted hover:text-foreground"}`}><Icon className="size-4 shrink-0" /><span className="text-sm font-medium">{label}</span></button>)}</nav><main className="min-w-0">{tab === "agents" && <AgentConfigurationPanel api={agentApi} />}{tab === "instructions" && <MarkdownConfigurationPanel adapter={instructionAdapter} placeholder="写入当前 workspace 的项目约束…" saveLabel="保存 AGENTS.md" initialMode="split" />}{tab === "fileignore" && <WorkspaceFileIgnorePanel workspaceId={workspaceId} />}</main></div></div></div>;
}
