import { useMemo, useSyncExternalStore } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  authorizeMCPServer,
  cancelMCPAuthorization,
  testMCPServer,
  waitMCPAuthorization,
} from "@/lib/api/extensions";
import { MCPServerTestController } from "@/lib/mcp/server-test-controller";
import { invalidateMcpServers } from "@/lib/query/extension-invalidation";

/** 配置页的测试控制器：绑定本作用域的接口，结束时重读服务列表（工作区列表含继承的系统级服务）。 */
export function useMCPServerTests(workspaceId: number | null) {
  const queryClient = useQueryClient();
  const controller = useMemo(() => new MCPServerTestController({
    test: (config) => testMCPServer(workspaceId, config),
    authorize: (config) => authorizeMCPServer(workspaceId, config),
    wait: waitMCPAuthorization,
    cancel: cancelMCPAuthorization,
    openBrowser: (url) => window.open(url, "_blank", "noopener,noreferrer"),
    onSettled: () => void invalidateMcpServers(queryClient, workspaceId),
  }), [workspaceId, queryClient]);
  const states = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
  return { controller, states };
}
