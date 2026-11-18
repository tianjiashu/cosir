import type { TransportToolDisplayData } from "../contract";

/** Result warnings are independent of the tool lifecycle; consumers must not turn them into Run failures. */
export function toolResultSignal(data: TransportToolDisplayData | null | undefined): string | null {
  if (!data) return null;
  switch (data.kind) {
    case "terminal-result":
      return data.end === "timed_out" ? "超时" : null;
    case "image-read":
      return Array.isArray(data.failures) && data.failures.length > 0 ? `${data.failures.length} 张失败` : null;
    case "browser-page": {
      // 停下的那步之后才有 skipped，所以只看 failed / unknown；unknown 是「可能已生效、须先看页面」，比失败更要留意。
      const statuses = Array.isArray(data.steps) ? data.steps.map((step) => (step as { status?: string } | null)?.status) : [];
      return statuses.includes("unknown") ? "结果不确定" : statuses.includes("failed") ? "动作未全部完成" : null;
    }
    case "web-fetch-urls":
      return data.partial === true ? "部分成功" : null;
    case "mcp-tool-result":
      return data.action === "list" && Array.isArray(data.servers)
        && data.servers.some((server) => typeof (server as { error?: unknown } | null)?.error === "string")
        ? "部分服务不可用" : null;
    default:
      return null;
  }
}
