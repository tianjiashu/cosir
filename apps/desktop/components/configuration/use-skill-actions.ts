import { useMutation, useQueryClient, type QueryClient } from "@tanstack/react-query";

import { setSkillsEnabled, type ExtensionScopeId, type Skill } from "@/lib/api/extensions";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";

/**
 * 写入或重新扫描后让受影响的查询重拉：工作区列表含继承来的系统级 Skill，所以系统级失效全部作用域
 * （各工作区页与输入框选择器），工作区只失效该工作区；详情、文件预览与分组目录都挂在同一前缀下。
 */
export function invalidateSkills(queryClient: QueryClient, workspaceId: ExtensionScopeId): Promise<void> {
  return queryClient.invalidateQueries({
    queryKey: workspaceId === null ? queryKeys.skillsAll : queryKeys.skills(workspaceId),
  });
}

/** Skills 列表页的路由；详情页是它下面的 `/<name>`。 */
export function skillsPath(workspaceId: ExtensionScopeId): string {
  return workspaceId === null ? "/settings/skills" : `/workspaces/${workspaceId}/settings/skills`;
}

export function skillDetailPath(workspaceId: ExtensionScopeId, name: string): string {
  return `${skillsPath(workspaceId)}/${encodeURIComponent(name)}`;
}

type EnabledChange = { names: string[]; enabled: boolean; label: string };

/**
 * 列表与详情共用的启停（单个与批量同一个接口，只写一次配置），成功后失效本作用域的 Skill 查询，以后端重读为准。
 * 没有删除：目录与 Codex 等工具共用、工作区目录在 git 里，真要删由用户在文件夹里做。
 */
export function useSkillActions(workspaceId: ExtensionScopeId) {
  const queryClient = useQueryClient();

  const enabledMutation = useMutation({
    mutationFn: ({ names, enabled }: EnabledChange) => setSkillsEnabled(workspaceId, names, enabled),
    onSuccess: (_result, { enabled, label }) => {
      void invalidateSkills(queryClient, workspaceId);
      notify.success(`已${enabled ? "启用" : "停用"} ${label}`);
    },
    onError: (error, { enabled, label }) => notify.error(`${enabled ? "启用" : "停用"} ${label} 失败`, { error, fallback: "保存 Skill 状态失败" }),
  });

  return {
    toggle: (skill: Skill) => enabledMutation.mutate({ names: [skill.name], enabled: !skill.enabled, label: skill.name }),
    setEnabled: (names: string[], enabled: boolean) =>
      enabledMutation.mutate({ names, enabled, label: names.length === 1 ? names[0] : `${names.length} 个 Skill` }),
    busy: enabledMutation.isPending,
  };
}
