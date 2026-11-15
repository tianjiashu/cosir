import type { MCPTransport } from "@/lib/api/extensions";

/** 传输方式给人看的叫法：列表行与编辑框共用这一份。 */
export const MCP_TRANSPORT_LABELS: Record<MCPTransport, string> = {
  stdio: "stdio",
  sse: "SSE",
  streamable_http: "Streamable HTTP",
};
