import { useEffect, useState } from "react";

import { discoverModels } from "@/lib/api/model-connections";
import { frontendLog } from "@/lib/logging/frontend-log";

export type ModelDiscovery =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; models: string[] }
  | { status: "unavailable"; reason: string };

/**
 * 按连接地址与 Key 拉取远端模型目录（输入停顿 450ms 后发起，改动即取消上一次）。
 *
 * 目录是表单增强：拉不到（接口不支持 `/models`、网络失败、返回为空）时给出原因，编辑卡退回手动输入，
 * 不阻断添加流程。
 */
export function useModelDiscovery(baseUrl: string, apiKey: string): ModelDiscovery {
  const [discovery, setDiscovery] = useState<ModelDiscovery>({ status: "idle" });

  useEffect(() => {
    const base_url = baseUrl.trim();
    const api_key = apiKey.trim();
    if (!base_url || !api_key) {
      setDiscovery({ status: "idle" });
      return;
    }
    setDiscovery({ status: "loading" });
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      discoverModels({ base_url, api_key }, { signal: controller.signal })
        .then(({ models }) => {
          setDiscovery(models.length > 0
            ? { status: "ready", models }
            : { status: "unavailable", reason: "接口没有返回模型列表，请手动填写模型名称" });
        })
        .catch((cause) => {
          if (controller.signal.aborted) return;
          setDiscovery({ status: "unavailable", reason: "未能获取模型列表，请手动填写模型名称" });
          void frontendLog("WARNING", "model_config_discovery_failed", "模型目录自动发现失败，回退为手动填写", {
            data: { base_url },
            error: cause,
          });
        });
    }, 450);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [apiKey, baseUrl]);

  return discovery;
}
