import { focusManager, QueryClient } from "@tanstack/react-query";

import { HttpError } from "@/lib/http/http-error";
import { onWindowAttentionChange, windowAttentive } from "@/lib/window-attention";

const MAX_QUERY_RETRIES = 2;

/**
 * 页面级服务端状态（配置、工具分组等）的唯一缓存。
 *
 * 边界：只管理普通领域 JSON API。Assistant Transport、对话快照与侧栏会话树各有
 * 自己的状态机和事实源（见 components/conversation-runtime/runtime/），不经由这里。
 *
 * 默认策略：
 * - 4xx 是确定性结果（不存在、冲突、校验失败），不重试；网络 / 5xx 最多重试 2 次。
 * - 桌面应用单窗口，不在窗口聚焦时自动重拉，避免编辑中途被后台刷新打断；
 *   后端换代后的统一失效由 useBackendQuerySync 负责。
 * - 「窗口聚焦」按 `window-attention` 的定义（可见且有焦点）：TanStack 默认只听可见性变化，桌面应用切到别的
 *   应用再切回时可见性不变，个别开启聚焦重读的查询会读不到外部改动。代价：失去焦点期间失败查询的重试暂停到回来。
 * - mutation 不自动重试：写操作是否可重放由各用例决定。
 */
export function createQueryClient(): QueryClient {
  // 没有可监听的 window 时（单测里的服务端渲染、替身 window）不监听，和 TanStack 默认监听的判断一致。
  focusManager.setEventListener((setFocused) => (
    typeof window === "undefined" || !window.addEventListener
      ? undefined
      : onWindowAttentionChange(() => setFocused(windowAttentive()))
  ));
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        refetchOnWindowFocus: false,
        retry: (failureCount, error) => {
          if (error instanceof HttpError && error.status >= 400 && error.status < 500) return false;
          return failureCount < MAX_QUERY_RETRIES;
        },
      },
      mutations: { retry: false },
    },
  });
}
