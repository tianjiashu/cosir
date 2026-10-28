import { useEffect, useRef, useState } from "react";

import { readFileStream } from "@/lib/api/workspace-files";

type StreamedBatch = { complete: boolean; next_offset?: number | null; error?: string };

export type StreamedSearchPhase = "idle" | "running" | "complete" | "stopped" | "failed";

/**
 * 文件区的流式分页搜索（文件名搜索与文件内查找共用）：按批累积结果、停止、重试与翻页。
 *
 * 由 `request` 驱动：它为 null 时空闲，内容变化（含 `generation`，用于「同一查询再搜一次」）即回到
 * 第一页重新搜索；body 原样发给后端，`offset` 由本 hook 追加。翻页记住访问过的页起点、按栈回退，
 * 不按前端假设的页大小倒推，后端调整每页条数不会让「上一页」错位。批次内容由 `decode` 在进程边界
 * 校验，带 `error` 的批次按失败处理。
 */
export function useStreamedSearch<Batch extends StreamedBatch, Item>({
  workspaceId,
  operation,
  request,
  decode,
  items: selectItems,
  onError,
}: {
  workspaceId: number;
  operation: string;
  request: { body: Record<string, unknown>; generation?: number } | null;
  decode: (value: unknown) => Batch;
  items: (batch: Batch) => Item[];
  /** 失败旁路（如契约错误打日志）；不影响返回的 phase / error。 */
  onError?: (error: unknown) => void;
}) {
  const requestKey = request === null ? null : JSON.stringify(request);
  const [pages, setPages] = useState<{ key: string | null; history: number[]; offset: number }>({
    key: requestKey,
    history: [],
    offset: 0,
  });
  // 请求换了就回到第一页：在渲染期对齐，避免先用旧偏移发一次请求。
  if (pages.key !== requestKey) setPages({ key: requestKey, history: [], offset: 0 });
  const offset = pages.key === requestKey ? pages.offset : 0;

  const [retry, setRetry] = useState(0);
  const [items, setItems] = useState<Item[]>([]);
  const [lastBatch, setLastBatch] = useState<Batch | null>(null);
  const [phase, setPhase] = useState<StreamedSearchPhase>("idle");
  const [error, setError] = useState<unknown>(null);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const control = useRef<AbortController | null>(null);
  const latest = useRef({ decode, selectItems, onError });
  latest.current = { decode, selectItems, onError };

  useEffect(() => {
    setItems([]);
    setLastBatch(null);
    setError(null);
    setNextOffset(null);
    if (requestKey === null) {
      setPhase("idle");
      return;
    }
    const body = (JSON.parse(requestKey) as { body: Record<string, unknown> }).body;
    const controller = new AbortController();
    control.current = controller;
    setPhase("running");
    void readFileStream<Batch>(
      workspaceId,
      operation,
      { ...body, offset },
      controller.signal,
      (batch) => {
        if (controller.signal.aborted) return;
        if (batch.error) throw new Error(batch.error);
        setItems((previous) => [...previous, ...latest.current.selectItems(batch)]);
        setLastBatch(batch);
        if (batch.complete) {
          setPhase("complete");
          setNextOffset(batch.next_offset ?? null);
        }
      },
      (value) => latest.current.decode(value),
    ).catch((cause: unknown) => {
      if (controller.signal.aborted) return;
      setPhase("failed");
      setError(cause);
      latest.current.onError?.(cause);
    });
    return () => controller.abort();
  }, [workspaceId, operation, requestKey, offset, retry]);

  return {
    items,
    lastBatch,
    phase,
    error,
    offset,
    hasPrevious: pages.history.length > 0,
    hasNext: nextOffset !== null,
    stop: () => {
      control.current?.abort();
      setPhase("stopped");
    },
    retry: () => setRetry((value) => value + 1),
    nextPage: () => {
      if (nextOffset === null) return;
      setPages({ key: requestKey, history: [...pages.history, offset], offset: nextOffset });
    },
    previousPage: () => {
      const previous = pages.history.at(-1);
      if (previous === undefined) return;
      setPages({ key: requestKey, history: pages.history.slice(0, -1), offset: previous });
    },
  };
}
