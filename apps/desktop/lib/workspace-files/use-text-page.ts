import { useCallback, useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { getText, type FileInfo } from "@/lib/api/workspace-files";
import { HttpError } from "@/lib/http/http-error";
import { queryKeys } from "@/lib/query/query-keys";
import { workspaceFiles, useFilesStore, type ReadingPosition } from "./workspace-files-store";

/**
 * 文本文件当前读到的那一段：分段位置与编码（按工作区记在 `workspace-files-store`）、读取查询，以及两条重置规则——
 * 版本变化回到开头；读取时版本已过期（409）回到开头并重读文件信息。源码 / Markdown 阅读器与表格阅读器共用，
 * 读取规则只在这里；各阅读器只决定怎么显示这一段。
 */
export function useTextPage(workspaceId: number, info: FileInfo) {
  const position = useFilesStore((s) => workspaceFiles(s.byWorkspace, workspaceId).positions[info.path]);
  const setStoredPosition = useFilesStore((s) => s.position);
  const queryClient = useQueryClient();
  const offset = position?.offset ?? 0;
  const encoding = position?.encoding ?? info.encoding ?? "utf-8";
  const tail = position?.tail ?? false;
  const before = position?.before;
  const setPosition = useCallback(
    (patch: Partial<ReadingPosition>) => setStoredPosition(workspaceId, info.path, patch),
    [setStoredPosition, workspaceId, info.path],
  );
  const previousVersion = useRef(info.version);
  useEffect(() => {
    if (previousVersion.current !== info.version) {
      setStoredPosition(workspaceId, info.path, { offset: 0, tail: false, before: undefined });
      previousVersion.current = info.version;
    }
  }, [info.version, info.path, workspaceId, setStoredPosition]);
  const page = useQuery({
    queryKey: queryKeys.fileText(workspaceId, info.path, info.version, encoding, offset, tail, before),
    queryFn: ({ signal }) => getText(workspaceId, info.path, offset, encoding, info.version, tail, before, signal),
    gcTime: 30_000,
  });
  useEffect(() => {
    if (page.error instanceof HttpError && page.error.status === 409) {
      setStoredPosition(workspaceId, info.path, { offset: 0 });
      void queryClient.invalidateQueries({ queryKey: queryKeys.fileInfo(workspaceId, info.path) });
    }
  }, [page.error, workspaceId, info.path, queryClient, setStoredPosition]);
  return {
    page,
    position,
    offset,
    encoding,
    tail,
    before,
    setPosition,
    /** 从 `next` 字节处读起，滚到顶。 */
    go: (next: number) => setPosition({ offset: next, before: undefined, tail: false, scrollTop: 0 }),
    /** 换编码从开头重读。 */
    setEncoding: (value: string) => setPosition({ encoding: value, offset: 0, tail: false, before: undefined }),
  };
}
