import type { TransportToolDisplayData } from "../contract";

/** Result warnings are independent of the tool lifecycle; consumers must not turn them into Run failures. */
export function toolResultSignal(data: TransportToolDisplayData | null | undefined): string | null {
  if (!data) return null;
  switch (data.kind) {
    case "terminal-result":
      return data.end === "timed_out" ? "超时" : null;
    case "image-read":
      return Array.isArray(data.failures) && data.failures.length > 0 ? "部分图片读取失败" : null;
    case "browser-page":
      return Array.isArray(data.steps) && data.steps.some((step) => {
        const status = (step as { status?: string } | null)?.status;
        return status === "failed" || status === "skipped";
      }) ? "动作未全部完成" : null;
    case "web-extract-urls":
      return data.partial === true ? "部分成功" : null;
    case "mcp-tool-result":
      return data.action === "list" && Array.isArray(data.servers)
        && data.servers.some((server) => typeof (server as { error?: unknown } | null)?.error === "string")
        ? "部分服务不可用" : null;
    default:
      return null;
  }
}
