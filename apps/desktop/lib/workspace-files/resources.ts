import { useCallback, useState } from "react";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { getApiBaseUrl } from "@/lib/http/client";
import { fileUrlPath, type FileInfo } from "@/lib/api/workspace-files";
import { newTraceId } from "@/lib/logging/trace-id";
import { frontendLog } from "@/lib/logging/frontend-log";
import { useBackendRuntime } from "@/lib/backend/use-backend-runtime";
import { queryKeys } from "@/lib/query/query-keys";

/**
 * 资源地址按版本寻址（后端要求必填），同一版本即同一内容，响应可长期缓存：`<img>` 直接用它，滚出视口再回来、
 * 重开同一张图都不再下载。版本过期时后端回 409，`confirmResourceFailure` 据此重读文件信息、换成新地址。
 */
export function fileResourceUrl(id: number, path: string, version: string, preview?: ImagePreview) {
  return `${getApiBaseUrl()}${fileUrlPath(id, "resource", { path, version, preview })}`;
}

/** 交付物用的缩略图（后端 `image_preview`）：`tile` 铺满方块，`card` 装进单张大图卡。只用于位图。 */
export type ImagePreview = "tile" | "card";

export function pdfResource(id: number, info: FileInfo) {
  return {
    url: fileResourceUrl(id, info.path, info.version),
    // 地址带版本、响应可长期缓存，不加 no-cache：分段读取命中缓存，版本变了地址自然换新。
    httpHeaders: { "X-Trace-Id": newTraceId() },
  };
}

/**
 * 图片的资源地址；版本未知（文件信息还没读到）时为空。订阅后端运行时快照，后端换代（端口变）时地址随之更新。
 * `<img>` 带不了 `X-Trace-Id`、也读不到错误响应体（与附件相同）：错误语义由文件信息查询承担。
 */
export function useFileResourceSrc(id: number, path: string, version: string | undefined) {
  useBackendRuntime();
  return version === undefined ? undefined : fileResourceUrl(id, path, version);
}

/**
 * 资源加载失败（`<img>` / `<audio>` / `<video>` 的 `onError`）的判定，规则只在这里。元素不告诉调用方原因，所以向同一
 * 地址再请求一次、只看状态码（不读响应体），按资源请求本身的结果分三种：
 *
 * - 请求不通（后端暂时不在）：不是内容的问题，不判失败；后端回来时换了端口，地址随之变化，元素自然重新加载。
 * - 409 版本过期、404 / 403 已不存在或无法访问：失效文件信息，换新地址，或交给文件信息查询的错误态显示。
 * - 其余（后端正常给出了内容，元素却显示不了）：内容本身显示不了，记日志并返回 true。
 *
 * 不能用「重读文件信息后版本没变」来推断：后端短暂重启时资源失败、信息重读又成功，版本没变，暂时的失败会被当成
 * 内容坏了。单个资源用 `useResourceLoadFailure`；一组资源（交付物方块，破图标由方块自己画）直接调它。
 */
export async function confirmResourceFailure(client: QueryClient, id: number, path: string, src: string): Promise<boolean> {
  const controller = new AbortController();
  let status: number;
  try {
    status = (await fetch(src, { cache: "no-store", headers: { "X-Trace-Id": newTraceId() }, signal: controller.signal })).status;
  } catch {
    return false;
  } finally {
    controller.abort();
  }
  if (status === 409 || status === 404 || status === 403) {
    await client.invalidateQueries({ queryKey: queryKeys.fileInfo(id, path), exact: true });
    return false;
  }
  void frontendLog("WARNING", "workspace_resource_load_failed", "工作区文件资源无法显示", { data: { workspaceId: id, path, status } });
  return true;
}

/**
 * 单个资源的加载失败态：`confirmResourceFailure` 判为显示不了后，同一地址保持失败；地址变了（换版本、后端换端口）
 * 自然复原，与方块里按地址记状态的做法一致。
 */
export function useResourceLoadFailure(id: number, path: string, src: string | undefined) {
  const client = useQueryClient();
  const [failedSrc, setFailedSrc] = useState<string | null>(null);
  const onError = useCallback(async () => {
    if (src !== undefined && (await confirmResourceFailure(client, id, path, src))) setFailedSrc(src);
  }, [client, id, path, src]);
  return { failed: src !== undefined && failedSrc === src, onError };
}
