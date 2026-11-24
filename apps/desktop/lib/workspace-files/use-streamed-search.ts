import { useEffect, useMemo, useRef, useState } from "react";

import { readFileStream } from "@/lib/api/workspace-files";

type StreamedBatch = { complete: boolean; next_offset?: number | null; error?: string };

export type StreamedSearchPhase = "idle" | "running" | "complete" | "stopped" | "failed";

/**
 * 工作区文件的流式分页搜索（文件面板的文件名搜索、文件内查找与输入框 @ 引用共用）：按批累积结果、停止、重试与翻页。
 *
 * 由 `request` 驱动：它为 null 时空闲，内容变化（含 `generation`，用于「同一查询再搜一次」）即回到
 * 第一页重新搜索；body 原样发给后端，`offset` 由本 hook 追加。翻页记住访问过的页起点、按栈回退，
 * 不按前端假设的页大小倒推，后端调整每页条数不会让「上一页」错位。批次内容由 `decode` 在进程边界
 * 校验，带 `error` 的批次按失败处理。
 *
 * 结果只属于发出它的那次请求：items、阶段、末批与下一页都连同所属请求（请求内容、页起点、重试次数）一起存放，
 * 对外只交出属于当前请求的；请求一换，同一次渲染里就是「空结果、进行中」，调用方拿不到上一个请求的结果。
 * 曾经各自是独立 state、到 effect 里才清空，换请求后有一帧旧结果；输入框 @ 引用另存防抖后的查询时，
 * 这个窗口拉长到防抖时长，回车插入了上一个查询的文件。
 *
 * `delayMs` 是防抖：请求一换立刻成为当前请求（旧结果马上作废），停稳 `delayMs` 后才真正发出；连续变化只发最后一次。
 */
export function useStreamedSearch<Batch extends StreamedBatch, Item>({
  workspaceId,
  operation,
  request,
  decode,
  items: selectItems,
  onError,
  delayMs = 0,
}: {
  workspaceId: number;
  operation: string;
  request: { body: Record<string, unknown>; generation?: number | string } | null;
  decode: (value: unknown) => Batch;
  items: (batch: Batch) => Item[];
  /** 失败旁路（如契约错误打日志）；不影响返回的 phase / error。 */
  onError?: (error: unknown) => void;
  /** 请求停稳多久后才发出（防抖），默认立即发出。 */
  delayMs?: number;
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
  // 一次具体的发出：请求内容、页起点与重试次数都算在内，结果按它归属。
  const fetchKey = requestKey === null ? null : JSON.stringify([workspaceId, operation, requestKey, offset, retry]);
  const [result, setResult] = useState<SearchResult<Batch, Item>>(() => emptyResult<Batch, Item>(null));
  // 引用稳定：没有新批次时同一请求返回同一份结果，调用方的 useMemo 不会白算。
  const current = useMemo(
    () => (result.key === fetchKey ? result : emptyResult<Batch, Item>(fetchKey)),
    [result, fetchKey],
  );
  const control = useRef<AbortController | null>(null);
  const latest = useRef({ decode, selectItems, onError });
  latest.current = { decode, selectItems, onError };

  useEffect(() => {
    if (fetchKey === null || requestKey === null) return;
    const body = (JSON.parse(requestKey) as { body: Record<string, unknown> }).body;
    const controller = new AbortController();
    control.current = controller;
    // 只改属于这次发出的结果；迟到的批次（已换请求）不会写进新请求。
    const update = (change: (previous: SearchResult<Batch, Item>) => Partial<SearchResult<Batch, Item>>) =>
      setResult((previous) => {
        const base = previous.key === fetchKey ? previous : emptyResult<Batch, Item>(fetchKey);
        return { ...base, ...change(base) };
      });
    const timer = setTimeout(() => {
      void readFileStream<Batch>(
        workspaceId,
        operation,
        { ...body, offset },
        controller.signal,
        (batch) => {
          if (controller.signal.aborted) return;
          if (batch.error) throw new Error(batch.error);
          update((previous) => ({
            items: [...previous.items, ...latest.current.selectItems(batch)],
            lastBatch: batch,
            ...(batch.complete ? { phase: "complete" as const, nextOffset: batch.next_offset ?? null } : {}),
          }));
        },
        (value) => latest.current.decode(value),
      ).catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        update(() => ({ phase: "failed", error: cause }));
        latest.current.onError?.(cause);
      });
    }, delayMs);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [fetchKey, requestKey, workspaceId, operation, offset, delayMs]);

  return {
    items: current.items,
    lastBatch: current.lastBatch,
    phase: current.phase,
    error: current.error,
    offset,
    hasPrevious: pages.history.length > 0,
    hasNext: current.nextOffset !== null,
    stop: () => {
      control.current?.abort();
      setResult({ ...current, phase: "stopped" });
    },
    retry: () => setRetry((value) => value + 1),
    nextPage: () => {
      if (current.nextOffset === null) return;
      setPages({ key: requestKey, history: [...pages.history, offset], offset: current.nextOffset });
    },
    previousPage: () => {
      const previous = pages.history.at(-1);
      if (previous === undefined) return;
      setPages({ key: requestKey, history: pages.history.slice(0, -1), offset: previous });
    },
  };
}

type SearchResult<Batch, Item> = {
  /** 结果所属的那次发出；与当前不同即作废。 */
  key: string | null;
  items: Item[];
  lastBatch: Batch | null;
  phase: StreamedSearchPhase;
  error: unknown;
  nextOffset: number | null;
};

/** 某次发出还没有任何结果时的样子：有请求就是「进行中」，没有请求就是「空闲」。 */
function emptyResult<Batch, Item>(key: string | null): SearchResult<Batch, Item> {
  return { key, items: [], lastBatch: null, phase: key === null ? "idle" : "running", error: null, nextOffset: null };
}
