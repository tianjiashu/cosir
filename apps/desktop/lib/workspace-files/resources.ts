import {
  getBackendRuntimeSnapshot,
  subscribeBackendRuntime,
} from "@/src/runtime-config";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { useEffect, useState, useSyncExternalStore } from "react";
import { getApiBaseUrl, requestOk } from "@/lib/http/client";
import { fileUrlPath, type FileInfo } from "@/lib/api/workspace-files";
import { newTraceId } from "@/lib/trace";

export function pdfResource(id: number, info: FileInfo) {
  return {
    url: `${getApiBaseUrl()}${fileUrlPath(id, "resource", { path: info.path, version: info.version })}`,
    httpHeaders: { "X-Trace-Id": newTraceId(), "Cache-Control": "no-cache" },
  };
}

/** 图片使用 blob URL，挂载周期内持有，切换文件立即取消网络并释放 URL。 */
export function useImageResource(
  id: number,
  path: string,
  version: string | undefined,
) {
  const backend = useSyncExternalStore(
    subscribeBackendRuntime,
    getBackendRuntimeSnapshot,
    getBackendRuntimeSnapshot,
  );
  const [state, setState] = useState<{
    url?: string;
    error?: string;
    thumbnail?: boolean;
  }>({});
  useEffect(() => {
    if (version === undefined) return;
    const controller = new AbortController();
    let url: string | undefined;
    setState({});
    void (async () => {
      try {
        const response = await requestOk(
          fileUrlPath(id, "resource", { path, version }),
          { signal: controller.signal },
        );
        const blob = await response.blob();
        if (controller.signal.aborted) return;
        url = URL.createObjectURL(blob);
        setState({
          url,
          thumbnail:
            response.headers.get("X-Cosir-Image-Preview") === "thumbnail",
        });
      } catch (error) {
        if (!controller.signal.aborted)
          setState({ error: userErrorMessage(error, "图片读取失败") });
      }
    })();
    return () => {
      controller.abort();
      if (url) URL.revokeObjectURL(url);
    };
  }, [id, path, version, backend]);
  return state;
}
