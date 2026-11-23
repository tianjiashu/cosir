import { useEffect, useState } from "react";
import { getApiBaseUrl, requestJson } from "@/lib/http/client";

/** 本机终端沙箱能力（后端 `GET /terminal/sandbox`）；`settled` 为假表示探测没有结论，不能据此判定不支持。 */
export type SandboxCapability = { supported: boolean; reason: string; settled: boolean };

// 机器级事实：每代后端（地址不同）取一次。只缓存有结论的结果，与后端同一口径；请求失败下次重试。
let cached: { base: string; capability: SandboxCapability } | null = null;
let inflight: { base: string; promise: Promise<SandboxCapability> } | null = null;

export function loadSandboxCapability(): Promise<SandboxCapability> {
  const base = getApiBaseUrl();
  if (cached?.base === base) return Promise.resolve(cached.capability);
  if (inflight?.base === base) return inflight.promise;
  const promise = requestJson<SandboxCapability>("/terminal/sandbox").then((capability) => {
    if (capability.settled) cached = { base, capability };
    return capability;
  }).finally(() => { if (inflight?.promise === promise) inflight = null; });
  inflight = { base, promise };
  return promise;
}

/** 选择器读它：`null` 表示还没取到或取失败（失败只记在这里，不与切换错误混在一起）。 */
export function useSandboxCapability(): { capability: SandboxCapability | null; error: string } {
  const [state, setState] = useState<{ capability: SandboxCapability | null; error: string }>(() => ({ capability: cached?.base === getApiBaseUrl() ? cached.capability : null, error: "" }));
  useEffect(() => {
    let alive = true;
    loadSandboxCapability().then(
      (capability) => { if (alive) setState({ capability, error: "" }); },
      () => { if (alive) setState({ capability: null, error: "无法检查终端沙箱" }); },
    );
    return () => { alive = false; };
  }, []);
  return state;
}
