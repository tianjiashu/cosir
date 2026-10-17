import {
  useIsMutating,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  getBrowserVisibility,
  changeBrowserVisibility,
} from "@/lib/api/workspace-files";
import { queryKeys } from "@/lib/query/query-keys";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";

/** 查询缓存只承载后端配置；版本变化使目录分页和搜索重新读取，阅读标签不参与。 */
export function useBrowserVisibility(workspaceId: number, enabled = true) {
  return useQuery({
    queryKey: queryKeys.browserVisibility(workspaceId),
    queryFn: ({ signal }) => getBrowserVisibility(workspaceId, signal),
    enabled,
    staleTime: 15_000,
    // @ 候选也可能独立于文件面板使用；此时仍能收敛外部配置修改。
    refetchInterval: enabled ? 15_000 : false,
  });
}

export function useChangeBrowserVisibility(workspaceId: number) {
  const client = useQueryClient();
  const key = queryKeys.browserVisibility(workspaceId);
  const pending = useIsMutating({ mutationKey: key }) > 0;
  const mutation = useMutation({
    mutationKey: key,
    scope: { id: `browser-visibility-${workspaceId}` },
    mutationFn: ({ paths, ignored }: { paths: string[]; ignored: boolean }) =>
      changeBrowserVisibility(workspaceId, paths, ignored),
    onSuccess: async (result) => {
      await client.cancelQueries({ queryKey: key });
      client.setQueryData(key, result.settings);
    },
  });
  const change = async (
    paths: string[],
    ignored: boolean,
  ): Promise<boolean> => {
    try {
      const result = await mutation.mutateAsync({ paths, ignored });
      if (result.changed_paths.length) {
        notify.success(
          ignored
            ? `已忽略 ${paths.length === 1 ? paths[0].split("/").at(-1) : `${result.changed_paths.length} 项`}`
            : "已恢复显示",
          {
            description: ignored ? "仅影响文件浏览器和界面搜索" : undefined,
            action: ignored
              ? {
                  label: "撤销",
                  onClick: () => {
                    void change(result.changed_paths, false);
                  },
                }
              : undefined,
          },
        );
      }
      return true;
    } catch (error) {
      void frontendLog(
        "ERROR",
        "workspace_browser_visibility_failed",
        "更新文件浏览器忽略设置失败",
        {
          data: { workspaceId, paths, ignored },
          error,
        },
      );
      notify.error(ignored ? "忽略失败" : "恢复显示失败", {
        error,
        action: {
          label: "重试",
          onClick: () => {
            void change(paths, ignored);
          },
        },
      });
      return false;
    }
  };
  return { change, pending };
}
