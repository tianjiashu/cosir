import { useCallback, useState } from "react";
import { useMutation, useQuery, useQueryClient, type QueryKey } from "@tanstack/react-query";

import type { RequestOptions } from "@/lib/http/client";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";

export type ConfigurationDocumentAdapter<TDocument extends { content: string }> = {
  /** 文档的查询键（来自 queryKeys），同时决定缓存共享与失效范围。 */
  queryKey: QueryKey;
  /** 面向用户的文档名，用于保存提示与未保存修改提示。 */
  label: string;
  load: (options?: RequestOptions) => Promise<TDocument>;
  save: (content: string) => Promise<TDocument>;
  /** 保存失败的日志事件名：各文档沿用自己的事件名，排查时按它检索。 */
  saveFailedEvent: string;
};

type ConfigurationDocumentState<TDocument> = {
  document: TDocument | undefined;
  content: string;
  loadError: unknown;
  saving: boolean;
  dirty: boolean;
  /** 最近一次保存失败的原始错误；编辑后清除。展示文案由调用方经 userErrorMessage 转换。 */
  saveError: unknown;
  reload: () => void;
  setContent: (content: string) => void;
  save: () => void;
};

/**
 * 管理单文本配置文档（正文是一段文本，如 Markdown 指令、.fileignore）的读取、编辑副本与保存。
 *
 * 该 hook 不理解作用域，也不直接访问 HTTP；调用方通过 adapter 注入具体 API 与查询键。后端文档
 * （查询缓存）是事实源，本地只保存用户尚未保存的草稿：草稿为 null 时正文跟随服务端，因此后端
 * 换代后的重新读取不会覆盖正在编辑的内容，保存成功后以服务端返回的正文为准（后端可能规范化内容）。
 * 能否保存（预算、非空）是各面板的界面规则，由面板判定后再调用 `save`。
 */
export function useConfigurationDocument<TDocument extends { content: string }>(
  adapter: ConfigurationDocumentAdapter<TDocument>,
): ConfigurationDocumentState<TDocument> {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<string | null>(null);
  const query = useQuery({
    queryKey: adapter.queryKey,
    queryFn: ({ signal }) => adapter.load({ signal }),
  });
  const mutation = useMutation({
    mutationFn: adapter.save,
    onSuccess: (saved, submitted) => {
      queryClient.setQueryData(adapter.queryKey, saved);
      // 保存期间用户若继续编辑，保留新草稿；否则回到跟随服务端正文。
      setDraft((current) => (current === submitted ? null : current));
      notify.success(`${adapter.label}已保存`);
    },
    onError: (error) => {
      void frontendLog("ERROR", adapter.saveFailedEvent, `保存${adapter.label}失败`, {
        data: { queryKey: adapter.queryKey },
        error,
      });
    },
  });

  const document = query.data;
  const content = draft ?? document?.content ?? "";
  const { mutate, reset, isPending, isError } = mutation;

  const setContent = useCallback((nextContent: string) => {
    setDraft(nextContent);
    // 只清除失败提示；保存中不能 reset，否则观察者脱离进行中的请求，按钮会提前解锁。
    if (isError) reset();
  }, [isError, reset]);

  // 保存中或文档尚未读取时忽略，避免重复提交。
  const save = useCallback(() => {
    if (isPending || !document) return;
    mutate(content);
  }, [content, document, isPending, mutate]);

  return {
    document,
    content,
    loadError: query.error,
    saving: isPending,
    // 草稿只能在文档读取后经编辑器写入，所以草稿非空时 document 必已存在。
    dirty: draft !== null && draft !== document?.content,
    saveError: mutation.error,
    reload: () => void query.refetch(),
    setContent,
    save,
  };
}
