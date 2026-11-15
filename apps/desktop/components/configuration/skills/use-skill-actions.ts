import { useMutation, useQueryClient } from "@tanstack/react-query";

import { setSkillsEnabled } from "@/lib/api/extensions";
import { notify } from "@/lib/notify";
import { invalidateSkills } from "@/lib/query/extension-invalidation";

type EnabledChange = { names: string[]; enabled: boolean };

const changeLabel = (names: string[]) => (names.length === 1 ? names[0] : `${names.length} 个 Skill`);

/**
 * 列表与详情共用的启停（单个与批量同一个接口，只写一次配置），成功后失效本作用域的 Skill 查询，以后端重读为准。
 * 没有删除：目录与 Codex 等工具共用、工作区目录在 git 里，真要删由用户在文件夹里做。
 */
export function useSkillActions(workspaceId: number | null) {
  const queryClient = useQueryClient();

  const enabledMutation = useMutation({
    mutationFn: ({ names, enabled }: EnabledChange) => setSkillsEnabled(workspaceId, names, enabled),
    onSuccess: (_result, { names, enabled }) => {
      void invalidateSkills(queryClient, workspaceId);
      notify.success(`已${enabled ? "启用" : "停用"} ${changeLabel(names)}`);
    },
    onError: (error, { names, enabled }) => notify.error(`${enabled ? "启用" : "停用"} ${changeLabel(names)} 失败`, { error, fallback: "保存 Skill 状态失败" }),
  });

  return {
    setEnabled: (names: string[], enabled: boolean) => enabledMutation.mutate({ names, enabled }),
    busy: enabledMutation.isPending,
  };
}
