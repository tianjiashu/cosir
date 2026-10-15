"use client";

import { useCallback, useState } from "react";
import { useMutation, useQuery, useQueryClient, type QueryKey } from "@tanstack/react-query";

import { UserFacingError } from "@/lib/errors/user-facing-error";
import type { RequestOptions } from "@/lib/http/client";
import { frontendLog } from "@/lib/logging/frontend-log";
import type { MarkdownDocumentContract } from "@/lib/markdown-document";
import { notify } from "@/lib/notify";

export type MarkdownConfigurationAdapter<TDocument extends MarkdownDocumentContract> = {
  /** 文档的查询键（来自 queryKeys），同时决定缓存共享与失效范围。 */
  queryKey: QueryKey;
  /** 面向用户的文档名，用于保存提示与未保存修改提示。 */
  label: string;
  load: (options?: RequestOptions) => Promise<TDocument>;
  save: (content: string) => Promise<TDocument>;
  allowEmpty?: boolean;
};

type MarkdownConfigurationState<TDocument extends MarkdownDocumentContract> = {
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
 * 管理 Markdown 配置文档的读取、编辑副本与保存。
 *
 * 该 hook 不理解 system/workspace 作用域，也不直接访问 HTTP；调用方通过 adapter 注入具体
 * API 与查询键。后端文档（查询缓存）是事实源，本地只保存用户尚未保存的草稿：草稿为 null
 * 时正文跟随服务端，因此后端换代后的重新读取不会覆盖用户正在编辑的内容，保存成功后
 * 以服务端返回的正文为准（后端可能规范化内容）。
 */
export function useMarkdownConfigurationDocument<
  TDocument extends MarkdownDocumentContract,
>(adapter: MarkdownConfigurationAdapter<TDocument>): MarkdownConfigurationState<TDocument> {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<string | null>(null);
  const query = useQuery({
    queryKey: adapter.queryKey,
    queryFn: ({ signal }) => adapter.load({ signal }),
  });
  const mutation = useMutation({
    mutationFn: async (content: string) => {
      if (!adapter.allowEmpty && !content.trim()) throw new UserFacingError("内容不能为空");
      return adapter.save(content);
    },
    onSuccess: (saved, submitted) => {
      queryClient.setQueryData(adapter.queryKey, saved);
      // 保存期间用户若继续编辑，保留新草稿；否则回到跟随服务端正文。
      setDraft((current) => (current === submitted ? null : current));
      notify.success(`${adapter.label}已保存`);
    },
    onError: (error) => {
      void frontendLog("ERROR", "markdown_configuration_save_failed", "保存 Markdown 配置失败", {
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

  // 快捷键与按钮共用：保存中或文档尚未读取时忽略，避免重复提交。
  const save = useCallback(() => {
    if (isPending || !document) return;
    mutate(content);
  }, [content, document, isPending, mutate]);

  return {
    document,
    content,
    loadError: query.error,
    saving: isPending,
    dirty: document !== undefined && draft !== null && draft !== document.content,
    saveError: mutation.error,
    reload: () => void query.refetch(),
    setContent,
    save,
  };
}
