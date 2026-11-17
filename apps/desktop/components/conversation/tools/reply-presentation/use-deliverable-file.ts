import { useQueries, useQuery, type UseQueryResult } from "@tanstack/react-query";

import { useReferenceBridge } from "@/components/workspace-references/reference-bridge";
import type { FileInfo } from "@/lib/api/workspace-files";
import type { TransportDeliverable } from "@/lib/assistant/contract";
import { fileInfoQueryOptions } from "@/lib/query/query-options";
import { fileReadProblem } from "@/lib/workspace-files/read-problem";

/** 交付物的现状：`info` 是现读的文件信息；`problem` 是读不到的原因（`gone` 已不存在、`denied` 无法访问、`unknown` 其他）。 */
export type DeliverableFileState = { info: FileInfo | null; problem: "gone" | "denied" | "unknown" | null };

const UNREAD: DeliverableFileState = { info: null, problem: null };

/** 只有工作区内的文件现读；目录、网址与工作区外的条目沿用交付时的记录。 */
function readablePath(deliverable: TransportDeliverable): string | null {
  return deliverable.type === "file" ? deliverable.workspacePath : null;
}

/** 先看错误：重拉失败时 react-query 保留上一次的 `data`，文件被删后若先看 `data` 会一直显示旧值（与 `FilePreview` 同序）。 */
function stateOf(query: UseQueryResult<FileInfo>): DeliverableFileState {
  if (!query.error) return query.data ? { info: query.data, problem: null } : UNREAD;
  const problem = fileReadProblem(query.error);
  return { info: null, problem: problem === "missing-file" ? "gone" : problem === "denied" ? "denied" : "unknown" };
}

/**
 * 现读工作区内交付文件的信息（大小、是否存在、图片版本与宽高）：与文件面板共用 `fileInfoQueryOptions` 的缓存，
 * 文件变化由工作区监听整体失效 `queryKeys.files` 前缀后自动刷新（会话登记见 `useSessionDeliverableWatch`）。
 * 工作区取当前会话所在的工作区（`ReferenceBridge`）；没有工作区时不读。
 */
export function useDeliverableFile(deliverable: TransportDeliverable): DeliverableFileState {
  const workspaceId = useReferenceBridge()?.workspaceId ?? null;
  const path = readablePath(deliverable);
  const query = useQuery({ ...fileInfoQueryOptions(workspaceId ?? 0, path ?? ""), enabled: workspaceId !== null && path !== null });
  return workspaceId === null || path === null ? UNREAD : stateOf(query);
}

/** 一组交付物各自的现状（图片方块一行用）；规则与 `useDeliverableFile` 相同。 */
export function useDeliverableFiles(deliverables: readonly TransportDeliverable[]): { workspaceId: number | null; states: DeliverableFileState[] } {
  const workspaceId = useReferenceBridge()?.workspaceId ?? null;
  const queries = useQueries({
    queries: deliverables.map((deliverable) => {
      const path = readablePath(deliverable);
      return { ...fileInfoQueryOptions(workspaceId ?? 0, path ?? ""), enabled: workspaceId !== null && path !== null };
    }),
  });
  return { workspaceId, states: queries.map((query, index) => (workspaceId === null || readablePath(deliverables[index]) === null ? UNREAD : stateOf(query))) };
}
