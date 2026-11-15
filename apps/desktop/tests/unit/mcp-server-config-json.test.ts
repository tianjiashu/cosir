import { describe, expect, it } from "vitest";

import {
  formatMcpServerJson,
  parseMcpServerJson,
} from "@/lib/mcp/server-config-json";
import type { MCPServerConfiguration } from "@/lib/api/extensions";

const stdioServer: MCPServerConfiguration = {
  name: "filesystem",
  transport: "stdio",
  command: "uvx",
  args: ["mcp-server-filesystem", "/workspace"],
  env: { API_KEY: "secret" },
  url: "",
  headers: {},
  oauth: null,
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

  it("uses the Claude Code oauth shape and keeps variable references verbatim", () => {
    const text = JSON.stringify({
      mcpServers: {
        github: {
          type: "http",
          url: "https://api.githubcopilot.com/mcp/",
          oauth: { clientId: "Iv1.abc", clientSecret: "${GITHUB_CLIENT_SECRET}", callbackPort: 8765 },
        },
      },
    });

    const server = parseMcpServerJson(text);

    expect(server.oauth).toEqual({ client_id: "Iv1.abc", client_secret: "${GITHUB_CLIENT_SECRET}", callback_port: 8765, scope: "" });
    expect(JSON.parse(formatMcpServerJson(server))).toEqual(JSON.parse(text));
  });

  it.each([
    ["invalid JSON", "{"],
    ["oauth on stdio", JSON.stringify({ mcpServers: { one: { command: "a", oauth: {} } } })],
    ["non-integer callback port", JSON.stringify({ mcpServers: { one: { type: "http", url: "https://x", oauth: { callbackPort: "80" } } } })],
    ["missing mcpServers", "{}"],
    ["multiple services", JSON.stringify({ mcpServers: { one: { command: "a" }, two: { command: "b" } } })],
    ["unsupported fields", JSON.stringify({ mcpServers: { one: { command: "a", cwd: "/tmp" } } })],
  ])("rejects %s without silently dropping configuration", (_caseName, text) => {
    expect(() => parseMcpServerJson(text)).toThrow();
  });
});
