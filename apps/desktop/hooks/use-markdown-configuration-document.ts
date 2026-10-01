"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { MarkdownDocumentContract } from "@/lib/markdown-document";

export type MarkdownConfigurationAdapter<TDocument extends MarkdownDocumentContract> = {
  load: () => Promise<TDocument>;
  save: (content: string) => Promise<TDocument>;
  allowEmpty?: boolean;
};

type MarkdownConfigurationState<TDocument extends MarkdownDocumentContract> = {
  document: TDocument | null;
  content: string;
  loading: boolean;
  saving: boolean;
  saved: boolean;
  dirty: boolean;
  error: string | null;
  load: () => Promise<void>;
  setContent: (content: string) => void;
  save: () => Promise<boolean>;
};

/**
 * 管理 Markdown 配置文档的加载、编辑和保存状态。
 *
 * 该 hook 不理解 system/workspace 作用域，也不直接访问 HTTP；调用方通过 adapter 注入具体
 * API。它只维护当前编辑副本，后端返回的文档仍是持久化事实源。
 */
export function useMarkdownConfigurationDocument<
  TDocument extends MarkdownDocumentContract,
>(adapter: MarkdownConfigurationAdapter<TDocument>): MarkdownConfigurationState<TDocument> {
  const [document, setDocument] = useState<TDocument | null>(null);
  const [content, setContentState] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const contentRef = useRef(content);
  const adapterRef = useRef(adapter);

  useEffect(() => {
    adapterRef.current = adapter;
  }, [adapter]);

  const setContent = useCallback((nextContent: string) => {
    contentRef.current = nextContent;
    setContentState(nextContent);
    setSaved(false);
    setError(null);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const nextDocument = await adapterRef.current.load();
      setDocument(nextDocument);
      contentRef.current = nextDocument.content;
      setContentState(nextDocument.content);
      setSaved(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取 Markdown 配置失败");
    } finally {
      setLoading(false);
    }
  }, []);

  const save = useCallback(async () => {
    const nextContent = contentRef.current;
    if (!adapterRef.current.allowEmpty && !nextContent.trim()) {
      setError("内容不能为空");
      return false;
    }
    setSaving(true);
    setSaved(false);
    setError(null);
    try {
      const nextDocument = await adapterRef.current.save(nextContent);
      setDocument(nextDocument);
      contentRef.current = nextDocument.content;
      setContentState(nextDocument.content);
      setSaved(true);
      return true;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存 Markdown 配置失败");
      return false;
    } finally {
      setSaving(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, adapter]);

  return {
    document,
    content,
    loading,
    saving,
    saved,
    dirty: document !== null && document.content !== content,
    error,
    load,
    setContent,
    save,
  };
}
