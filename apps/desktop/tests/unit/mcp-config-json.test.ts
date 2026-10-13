import { describe, expect, it } from "vitest";

import {
  formatMcpServerJson,
  parseMcpServerJson,
} from "@/components/configuration/mcp-config-json";
import type { MCPServerConfiguration } from "@/lib/api/system-extensions";

const stdioServer: MCPServerConfiguration = {
  name: "filesystem",
  transport: "stdio",
  command: "uvx",
  args: ["mcp-server-filesystem", "/workspace"],
  env: { API_KEY: "secret" },
  url: "",
  headers: {},
  enabled: false,
};

describe("MCP JSON configuration", () => {
  it("round-trips stdio fields and disabled state through mcpServers", () => {
    const json = formatMcpServerJson(stdioServer);

    expect(JSON.parse(json)).toEqual({
      mcpServers: {
        filesystem: {
          command: "uvx",
          args: ["mcp-server-filesystem", "/workspace"],
          env: { API_KEY: "secret" },
          enabled: false,
        },
      },
    });
    expect(parseMcpServerJson(json)).toEqual(stdioServer);
  });

  it.each([
    ["http", "streamable_http"],
    ["sse", "sse"],
  ] as const)("maps portable %s transport to %s", (type, transport) => {
    const server = parseMcpServerJson(JSON.stringify({
      mcpServers: {
        remote: { type, url: "https://example.com/mcp", headers: { Authorization: "Bearer token" } },
      },
    }));

    expect(server).toMatchObject({
      name: "remote",
      transport,
      url: "https://example.com/mcp",
      headers: { Authorization: "Bearer token" },
      enabled: true,
    });
  });

  it.each([
    ["invalid JSON", "{"],
    ["missing mcpServers", "{}"],
    ["multiple services", JSON.stringify({ mcpServers: { one: { command: "a" }, two: { command: "b" } } })],
    ["unsupported fields", JSON.stringify({ mcpServers: { one: { command: "a", cwd: "/tmp" } } })],
  ])("rejects %s without silently dropping configuration", (_caseName, text) => {
    expect(() => parseMcpServerJson(text)).toThrow();
  });
});
