import { requestJson, requestOk, jsonRequestInit } from "@/lib/http/client";
import { readNdjson } from "@/lib/http/ndjson";

export type FileEntry = {
  path: string;
  name: string;
  kind: "directory" | "file" | "unavailable";
  symlink: boolean;
  hint: string | null;
};
export type DirectoryPage = {
  path: string;
  entries: FileEntry[];
  total: number;
  hidden_count: number;
  next_cursor: string | null;
};
/**
 * 文件类型词表：与后端 `models/file_kind.py` 一一对应（判定只在后端 `file_classification`），文件面板与
 * 交付记录共用；快照校验也从这里取，前端只此一份。
 */
export const FILE_KINDS = ["text", "markdown", "table", "mermaid", "image", "svg", "pdf", "audio", "video", "binary"] as const;
export type FileKind = (typeof FILE_KINDS)[number];

export type FileInfo = {
  path: string;
  name: string;
  root_path: string;
  size: number;
  modified_ns: number;
  version: string;
  kind: FileKind;
  mime: string;
  encoding: string | null;
  /** 位图的原图尺寸（后端只读文件头）；非位图为 null，位图读不出（过大、损坏）也为 null。 */
  width: number | null;
  height: number | null;
  /** 操作系统能否出缩略图、快速查看（后端按最终类型与后缀表判定）；只对它为真的文件向宿主要缩略图。 */
  system_preview: boolean;
};

/**
 * 位图读不出：后端只读文件头取宽高，像素超限或文件损坏时宽高为空（`image_size`）。文件面板、Markdown 内嵌图与交付物
 * 方块都在请求资源前据此判定「无法预览」，不发一个注定失败的资源请求；规则只在这里。
 */
export function isUnreadableImage(info: Pick<FileInfo, "kind" | "width">): boolean {
  return info.kind === "image" && info.width === null;
}
export type TextPage = {
  text: string;
  offset: number;
  next_offset: number | null;
  size: number;
  version: string;
  encoding: string;
};
export type SearchBatch = {
  entries: FileEntry[];
  skipped: number;
  skipped_reasons: Record<string, number>;
  complete: boolean;
  next_offset?: number | null;
  error?: string;
};

/** 搜索流批次违反运行时协议时携带无敏感内容的字段定位信息。 */
export class SearchBatchContractError extends Error {
  constructor(readonly invalidFields: string[]) {
    super("搜索响应格式异常，请重试");
    this.name = "SearchBatchContractError";
  }
}

type FindTextMatch = { offset: number; snippet: string };
export type FindTextBatch = {
  matches: FindTextMatch[];
  complete: boolean;
  next_offset?: number | null;
  error?: string;
};

/** 两种搜索流共用的批次外壳：非对象直接拒绝；带 `error` 的批次原样交给调用方抛出。 */
function readBatchEnvelope(value: unknown): { batch: Record<string, unknown>; invalidFields: string[] } | null {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    throw new SearchBatchContractError(["batch"]);
  const batch = value as Record<string, unknown>;
  if (typeof batch.error === "string") return null;
  const invalidFields: string[] = [];
  if (typeof batch.complete !== "boolean") invalidFields.push("complete");
  if (
    batch.next_offset !== undefined &&
    batch.next_offset !== null &&
    (typeof batch.next_offset !== "number" ||
      !Number.isInteger(batch.next_offset))
  )
    invalidFields.push("next_offset");
  return { batch, invalidFields };
}

/** 在 NDJSON 进程边界校验文件名搜索批次；TypeScript 类型断言不会验证网络数据。 */
export function parseSearchBatch(value: unknown): SearchBatch {
  const envelope = readBatchEnvelope(value);
  if (envelope === null) return value as SearchBatch;
  const { batch, invalidFields } = envelope;
  if (!Array.isArray(batch.entries)) invalidFields.push("entries");
  if (!Number.isInteger(batch.skipped) || (batch.skipped as number) < 0)
    invalidFields.push("skipped");
  if (
    typeof batch.skipped_reasons !== "object" ||
    batch.skipped_reasons === null ||
    Array.isArray(batch.skipped_reasons) ||
    !Object.values(batch.skipped_reasons).every(
      (count) =>
        typeof count === "number" && Number.isInteger(count) && count >= 0,
    )
  )
    invalidFields.push("skipped_reasons");
  if (invalidFields.length) throw new SearchBatchContractError(invalidFields);
  return batch as SearchBatch;
}

/** 在 NDJSON 进程边界校验文件内查找批次。 */
export function parseFindTextBatch(value: unknown): FindTextBatch {
  const envelope = readBatchEnvelope(value);
  if (envelope === null) return value as FindTextBatch;
  const { batch, invalidFields } = envelope;
  if (
    !Array.isArray(batch.matches) ||
    !batch.matches.every(
      (match) =>
        typeof match === "object" && match !== null &&
        Number.isInteger((match as FindTextMatch).offset) &&
        typeof (match as FindTextMatch).snippet === "string",
    )
  )
    invalidFields.push("matches");
  if (invalidFields.length) throw new SearchBatchContractError(invalidFields);
  return batch as FindTextBatch;
}
export type WatchEvent = {
  type: "ready" | "changed" | "reconcile" | "heartbeat" | "closed";
  polling?: boolean;
};
const filesPath = (id: number, operation: string) =>
  `/workspaces/${id}/files/${operation}`;
export function fileUrlPath(
  id: number,
  operation: string,
  params: Record<string, string | number | boolean | undefined>,
) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params))
    if (value !== undefined) query.set(key, String(value));
  return `${filesPath(id, operation)}?${query}`;
}
export const getDirectory = (
  id: number,
  path: string,
  cursor: string | undefined,
  signal?: AbortSignal,
) =>
  requestJson<DirectoryPage>(fileUrlPath(id, "directory", { path, cursor }), {
    signal,
  });
export const getFileInfo = (id: number, path: string, signal?: AbortSignal) =>
  requestJson<FileInfo>(fileUrlPath(id, "info", { path }), { signal });
export const getText = (
  id: number,
  path: string,
  offset: number,
  encoding: string,
  version: string,
  tail: boolean,
  before: number | undefined,
  signal?: AbortSignal,
) =>
  requestJson<TextPage>(
    fileUrlPath(id, "text", { path, offset, encoding, version, tail, before }),
    { signal },
  );

/** JSON 行流仅传输查询结果与失效通知；decoder 在消费前验证协议，终止订阅会释放 reader。 */
export async function readFileStream<T>(
  id: number,
  operation: string,
  body: unknown,
  signal: AbortSignal,
  onValue: (value: T) => void,
  decode: (value: unknown) => T = (value) => value as T,
): Promise<void> {
  const response = await requestOk(
    filesPath(id, operation),
    jsonRequestInit(body, {
      method: "POST",
      signal,
      headers: { Accept: "application/x-ndjson" },
    }),
  );
  await readNdjson(response, onValue, decode);
}

export type BrowserVisibility = { ignored_paths: string[]; revision: string };
export type VisibilityChange = {
  settings: BrowserVisibility;
  changed_paths: string[];
};
export const getBrowserVisibility = (id: number, signal?: AbortSignal) =>
  requestJson<BrowserVisibility>(filesPath(id, "visibility"), { signal });
export const changeBrowserVisibility = (
  id: number,
  paths: string[],
  ignored: boolean,
) =>
  requestJson<VisibilityChange>(
    filesPath(id, "visibility"),
    jsonRequestInit({ paths, ignored }, { method: "PATCH" }),
  );
