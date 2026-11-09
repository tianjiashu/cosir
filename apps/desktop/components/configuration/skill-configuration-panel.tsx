import { skillsQueryOptions } from "@/lib/query/query-options";
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { Loader2Icon, PlusIcon, PuzzleIcon, Trash2Icon } from "lucide-react";

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
import { useMarkdownLinkComponents } from "@/components/links/markdown-links";
import { StaticMarkdown } from "@/components/markdown/static-markdown";
import {
  deleteSkill,
  getSkill,
  importSkill,
  ownScope,
  setSkillEnabled,
  type ExtensionScopeId,
  type Skill,
} from "@/lib/api/extensions";
import { confirmDelete } from "@/lib/confirm/confirm-store";
import { desktopHost } from "@/lib/desktop-host";
import { pickLocalFiles } from "@/lib/desktop-host/pick-local-files";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";

type ImportSource = "directory" | "archive";

/**
 * 写入后让受影响的列表重拉：工作区列表含继承来的系统级 Skill，所以系统级写入失效全部作用域
 * （各工作区页与输入框选择器），工作区写入只失效该工作区。
 */
function invalidateSkills(queryClient: QueryClient, workspaceId: ExtensionScopeId) {
  void queryClient.invalidateQueries({
    queryKey: workspaceId === null ? queryKeys.skillsAll : queryKeys.skills(workspaceId),
  });
}

/**
 * 一个作用域的 Skill 导入、启停与只读详情管理。`workspaceId` 为 `null` 时管理系统级 Skill；
 * 否则管理该工作区的 `.cosir/skills`，同时列出继承来的系统级 Skill（只读）。
 */
export function SkillConfigurationPanel({ workspaceId }: { workspaceId: ExtensionScopeId }) {
  const queryClient = useQueryClient();
  const scope = ownScope(workspaceId);
  const scopeLabel = workspaceId === null ? "系统 Skill 目录" : "工作区的 .cosir/skills 目录";
  // 原生文件选择框打开期间按钮尚未禁用，用同步标记挡住连击造成的重复弹框。
  const pickingSource = useRef(false);
  const [detailName, setDetailName] = useState<string | null>(null);
  const linkComponents = useMarkdownLinkComponents("configuration", { webOnly: true });

  const skillsQuery = useQuery({
    ...skillsQueryOptions(workspaceId),
  });
  const detailQuery = useQuery({
    queryKey: queryKeys.skill(workspaceId, detailName ?? ""),
    queryFn: ({ signal }) => getSkill(workspaceId, detailName!, { signal }),
    enabled: detailName !== null,
  });

  const toggleMutation = useMutation({
    mutationFn: (skill: Skill) => setSkillEnabled(workspaceId, skill.name, !skill.enabled),
    onSuccess: (_updated, skill) => {
      invalidateSkills(queryClient, workspaceId);
      notify.success(`已${skill.enabled ? "停用" : "启用"} ${skill.name}`);
    },
    onError: (error, skill) => notify.error(`${skill.enabled ? "停用" : "启用"} ${skill.name} 失败`, { error, fallback: "保存 Skill 状态失败" }),
  });

  const importMutation = useMutation({
    mutationFn: ({ sourcePath, sourceType }: { sourcePath: string; sourceType: ImportSource }) =>
      importSkill(workspaceId, sourcePath, sourceType),
    onSuccess: (skill) => {
      invalidateSkills(queryClient, workspaceId);
      notify.success(`已导入 ${skill.name}`, {
        description: skill.overrides ? `文件已复制到${scopeLabel}，并覆盖系统级同名 Skill。` : `文件已复制到${scopeLabel}。`,
      });
    },
    onError: (error) => notify.error("导入 Skill 失败", { error, fallback: "导入 Skill 失败，请检查所选目录或 ZIP" }),
  });

  const deleteMutation = useMutation({
    mutationFn: (name: string) => deleteSkill(workspaceId, name),
    onSuccess: (_result, name) => {
      invalidateSkills(queryClient, workspaceId);
      if (detailName === name) setDetailName(null);
      notify.success(`已删除 ${name}`);
    },
  });

  const busy = toggleMutation.isPending || deleteMutation.isPending || importMutation.isPending;

  async function selectAndImport(sourceType: ImportSource) {
    if (pickingSource.current || importMutation.isPending) return;
    pickingSource.current = true;
    let sourcePath: string | null;
    try {
      if (!desktopHost) throw new UserFacingError("只能在桌面应用中导入 Skill");
      sourcePath = sourceType === "directory"
        ? await desktopHost.dialog.openDirectory({ title: "选择要导入的 Skill 文件夹" })
        : (await pickLocalFiles({ accept: ".zip" }))[0]?.path || null;
    } catch (error) {
      void frontendLog("ERROR", "skill_import_picker_failed", "打开 Skill 导入选择框失败", { data: { sourceType }, error });
      notify.error("无法打开文件选择框", { error, fallback: "请重试或重启应用" });
      return;
    } finally {
      pickingSource.current = false;
    }
    if (!sourcePath) return;
    importMutation.mutate({ sourcePath, sourceType });
  }

  async function removeSkill(skill: Skill) {
    await confirmDelete({
      target: `Skill「${skill.name}」`,
      warning: skill.overrides
        ? `${scopeLabel}中的文件会一并删除，之后改用系统级同名 Skill。`
        : `${scopeLabel}中的文件会一并删除。`,
      action: () => deleteMutation.mutateAsync(skill.name),
      failureFallback: "删除 Skill 失败",
    });
  }

  return (
    <section className="space-y-5" aria-labelledby="skill-configuration-heading">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1.5">
          <h2 id="skill-configuration-heading" className="text-lg font-semibold">Skills</h2>
          {workspaceId !== null && (
            <p className="text-muted-foreground text-sm">导入到本工作区的 Skill 只在这里可用；与系统级同名时，本工作区的版本优先。</p>
          )}
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
          description={`导入本机 Skill 文件夹后会复制到${scopeLabel}，可以在这里启用、停用或查看内容。`}
        />
      ) : (
        <div className="space-y-3">
          {skillsQuery.data.map((skill) => {
            // 工作区里继承来的系统级 Skill 只读：启停与删除要去系统设置。
            const inherited = skill.scope !== scope;
            return (
              <Card key={skill.name} className="flex flex-wrap items-start gap-3 p-4">
                <PuzzleIcon className="text-muted-foreground mt-0.5 size-4 shrink-0" />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <button type="button" className="truncate text-left text-sm font-medium hover:underline" onClick={() => setDetailName(skill.name)}>{skill.name}</button>
                    <Badge size="sm" tone={skill.enabled ? "primary" : "neutral"}>
                      {skill.enabled ? "已启用" : "已停用"}
                    </Badge>
                    {inherited && <Badge size="sm" tone="neutral">系统继承</Badge>}
                    {skill.overrides && <Badge size="sm" tone="neutral">覆盖系统同名</Badge>}
                  </div>
                  <p className="text-muted-foreground mt-1 line-clamp-2 text-xs">{skill.description}</p>
                  <Button variant="link" size="sm" className="mt-1 h-auto px-0 py-0 text-xs" onClick={() => setDetailName(skill.name)}>查看说明</Button>
                </div>
                {inherited ? (
                  <span className="text-muted-foreground self-center text-xs">在系统设置中管理</span>
                ) : <>
                  <Switch checked={skill.enabled} aria-label={`${skill.enabled ? "停用" : "启用"} Skill ${skill.name}`} disabled={busy} onClick={() => toggleMutation.mutate(skill)} />
                  <Button variant="ghost" size="icon" aria-label={`删除 Skill ${skill.name}`} disabled={busy} onClick={() => void removeSkill(skill)}><Trash2Icon /></Button>
                </>}
              </Card>
            );
          })}
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
              <StaticMarkdown components={linkComponents} className="!space-y-3">{detailQuery.data.content}</StaticMarkdown>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </section>
  );
}
