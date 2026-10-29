"use client";

import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { open } from "@tauri-apps/plugin-dialog";
import { Loader2Icon, PlusIcon, PuzzleIcon, Trash2Icon } from "lucide-react";
import { Streamdown } from "streamdown";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/state-panel";
import { Switch } from "@/components/ui/switch";
import { markdownPlugins, useMermaidOptions } from "@/components/markdown-rendering-config";
import {
  deleteSystemSkill,
  getSystemSkill,
  getSystemSkills,
  importSystemSkill,
  saveEnabledSkills,
  type SystemSkill,
} from "@/lib/api/system-extensions";
import { confirmDelete } from "@/lib/confirm/confirm-store";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";

type ImportSource = "directory" | "archive";

/** 系统目录 Skill 的导入、启停与只读详情管理。 */
export function SkillConfigurationPanel() {
  const queryClient = useQueryClient();
  // 原生文件选择框打开期间按钮尚未禁用，用同步标记挡住连击造成的重复弹框。
  const pickingSource = useRef(false);
  const [detailName, setDetailName] = useState<string | null>(null);
  const mermaidOptions = useMermaidOptions();

  const skillsQuery = useQuery({
    queryKey: queryKeys.skills(),
    queryFn: ({ signal }) => getSystemSkills({ signal }),
  });
  const detailQuery = useQuery({
    queryKey: queryKeys.skill(detailName ?? ""),
    queryFn: ({ signal }) => getSystemSkill(detailName!, { signal }),
    enabled: detailName !== null,
  });

  // 启停提交的是完整启用列表，同一时刻只允许一次写入，避免基于旧列表互相覆盖。
  const toggleMutation = useMutation({
    mutationFn: (skill: SystemSkill) => {
      const enabledNames = (skillsQuery.data ?? [])
        .filter((item) => item.name === skill.name ? !item.enabled : item.enabled)
        .map((item) => item.name);
      return saveEnabledSkills(enabledNames);
    },
    onSuccess: (skills, skill) => {
      queryClient.setQueryData(queryKeys.skills(), skills);
      notify.success(`已${skill.enabled ? "停用" : "启用"} ${skill.name}`);
    },
    onError: (error, skill) => notify.error(`${skill.enabled ? "停用" : "启用"} ${skill.name} 失败`, { error, fallback: "保存 Skill 状态失败" }),
  });

  const importMutation = useMutation({
    mutationFn: ({ sourcePath, sourceType }: { sourcePath: string; sourceType: ImportSource }) =>
      importSystemSkill(sourcePath, sourceType),
    onSuccess: (skill) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.skills() });
      void queryClient.invalidateQueries({ queryKey: queryKeys.skill(skill.name) });
      notify.success(`已导入 ${skill.name}`, { description: "文件已复制到系统 Skill 目录。" });
    },
    onError: (error) => notify.error("导入 Skill 失败", { error, fallback: "导入 Skill 失败，请检查所选目录或 ZIP" }),
  });

  const deleteMutation = useMutation({
    mutationFn: deleteSystemSkill,
    onSuccess: (_result, name) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.skills() });
      queryClient.removeQueries({ queryKey: queryKeys.skill(name) });
      if (detailName === name) setDetailName(null);
      notify.success(`已删除 ${name}`);
    },
  });

  const busy = toggleMutation.isPending || deleteMutation.isPending || importMutation.isPending;

  async function selectAndImport(sourceType: ImportSource) {
    if (pickingSource.current || importMutation.isPending) return;
    pickingSource.current = true;
    let sourcePath: string | string[] | null;
    try {
      sourcePath = sourceType === "directory"
        ? await open({ directory: true, multiple: false, title: "选择要导入的 Skill 文件夹" })
        : await open({
            multiple: false,
            title: "选择要导入的 Skill ZIP",
            filters: [{ name: "Skill ZIP", extensions: ["zip"] }],
          });
    } catch (error) {
      void frontendLog("ERROR", "skill_import_picker_failed", "打开 Skill 导入选择框失败", { data: { sourceType }, error });
      notify.error("无法打开文件选择框", { error, fallback: "请重试或重启应用" });
      return;
    } finally {
      pickingSource.current = false;
    }
    if (typeof sourcePath !== "string") return;
    importMutation.mutate({ sourcePath, sourceType });
  }

  async function removeSkill(skill: SystemSkill) {
    await confirmDelete({
      target: `Skill「${skill.name}」`,
      warning: "系统 Skill 目录中的文件会一并删除。",
      action: () => deleteMutation.mutateAsync(skill.name),
      failureFallback: "删除 Skill 失败",
    });
  }

  return (
    <section className="space-y-5" aria-labelledby="skill-configuration-heading">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1.5">
          <h2 id="skill-configuration-heading" className="text-lg font-semibold">Skills</h2>
        </div>
        <div className="flex gap-2">
          <Button onClick={() => void selectAndImport("directory")} disabled={!skillsQuery.data || importMutation.isPending}>
            {importMutation.isPending ? <Loader2Icon className="animate-spin" /> : <PlusIcon />}
            导入 Skill 文件夹
          </Button>
          <Button variant="outline" onClick={() => void selectAndImport("archive")} disabled={!skillsQuery.data || importMutation.isPending}>
            {importMutation.isPending ? <Loader2Icon className="animate-spin" /> : <PlusIcon />}
            导入 ZIP
          </Button>
        </div>
      </header>

      {skillsQuery.isPending ? (
        <LoadingState label="正在读取 Skills…" />
      ) : skillsQuery.isError ? (
        <ErrorState
          message={userErrorMessage(skillsQuery.error, "读取 Skill 列表失败")}
          onRetry={() => void skillsQuery.refetch()}
          retryLabel="重新读取"
        />
      ) : skillsQuery.data.length === 0 ? (
        <EmptyState
          icon={<PuzzleIcon />}
          title="还没有 Skill"
          description="导入本机 Skill 文件夹后，可以在这里启用、停用或查看内容。"
        />
      ) : (
        <div className="space-y-3">
          {skillsQuery.data.map((skill) => (
            <Card key={skill.name} className="flex flex-wrap items-start gap-3 p-4">
              <PuzzleIcon className="text-muted-foreground mt-0.5 size-4 shrink-0" />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <button type="button" className="truncate text-left text-sm font-medium hover:underline" onClick={() => setDetailName(skill.name)}>{skill.name}</button>
                  <Badge size="sm" tone={skill.enabled ? "primary" : "neutral"}>
                    {skill.enabled ? "已启用" : "已停用"}
                  </Badge>
                </div>
                <p className="text-muted-foreground mt-1 line-clamp-2 text-xs">{skill.description}</p>
                <Button variant="link" size="sm" className="mt-1 h-auto px-0 py-0 text-xs" onClick={() => setDetailName(skill.name)}>查看说明</Button>
              </div>
              <Switch checked={skill.enabled} aria-label={`${skill.enabled ? "停用" : "启用"} Skill ${skill.name}`} disabled={busy} onClick={() => toggleMutation.mutate(skill)} />
              <Button variant="ghost" size="icon" aria-label={`删除 Skill ${skill.name}`} disabled={busy} onClick={() => void removeSkill(skill)}><Trash2Icon /></Button>
            </Card>
          ))}
        </div>
      )}

      <Dialog open={detailName !== null} onOpenChange={(open) => { if (!open) setDetailName(null); }}>
        <DialogContent className="max-h-[85vh] max-w-3xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{detailQuery.data?.name ?? "Skill 说明"}</DialogTitle>
            <DialogDescription>{detailQuery.data?.description ?? "只读预览 Skill.md 内容。"}</DialogDescription>
          </DialogHeader>
          {detailQuery.isPending ? (
            <LoadingState label="正在读取说明…" className="py-8" />
          ) : detailQuery.isError ? (
            <ErrorState
              message={userErrorMessage(detailQuery.error, "读取 Skill 详情失败")}
              onRetry={() => void detailQuery.refetch()}
            />
          ) : (
            <div className="aui-md max-h-[60vh] overflow-y-auto rounded-lg border bg-muted/20 p-4 leading-6">
              <Streamdown mode="static" plugins={markdownPlugins} shikiTheme={["github-light", "github-dark"]} linkSafety={{ enabled: true }} mermaid={mermaidOptions} className="!space-y-3">
                {detailQuery.data.content}
              </Streamdown>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </section>
  );
}
