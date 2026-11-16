import { useCallback, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { getApiBaseUrl } from "@/lib/http/client";
import { fileUrlPath, type FileInfo } from "@/lib/api/workspace-files";
import { newTraceId } from "@/lib/logging/trace-id";
import { frontendLog } from "@/lib/logging/frontend-log";
import { useBackendRuntime } from "@/lib/backend/use-backend-runtime";
import { queryKeys } from "@/lib/query/query-keys";

/**
 * 资源地址按版本寻址（后端要求必填），同一版本即同一内容，响应可长期缓存：`<img>` 直接用它，滚出视口再回来、
 * 重开同一张图都不再下载。版本过期时后端回 409，`useImageLoadFailure` 重读文件信息换成新地址。
 */
function fileResourceUrl(id: number, path: string, version: string) {
  return `${getApiBaseUrl()}${fileUrlPath(id, "resource", { path, version })}`;
}

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
 * `<img onError>` 的统一处理：先重读一次文件信息（最常见的原因是版本过期，重读后地址自然换新），
 * 重读后版本没变才判为「显示不了」并记日志。文件已不存在时由文件信息查询的错误态接手显示。
 */
export function useImageLoadFailure(id: number, path: string, version: string | undefined) {
  const client = useQueryClient();
  const [failedVersion, setFailedVersion] = useState<string | null>(null);
  const onError = useCallback(async () => {
    const key = queryKeys.fileInfo(id, path);
    await client.invalidateQueries({ queryKey: key, exact: true });
    if (client.getQueryData<FileInfo>(key)?.version !== version) return;
    setFailedVersion(version ?? null);
    void frontendLog("WARNING", "workspace_image_load_failed", "工作区图片无法显示", { data: { workspaceId: id, path } });
  }, [client, id, path, version]);
  return { failed: version !== undefined && failedVersion === version, onError };
}
