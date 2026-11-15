import { describe, expect, it } from "vitest";

import type { MCPServerConfiguration } from "@/lib/api/extensions";
import { emptyDraft, fromDraft, toDraft } from "@/lib/mcp/server-draft";

const remote: MCPServerConfiguration = {
  name: "github",
  transport: "streamable_http",
  command: "",
  args: [],
  env: {},
  url: "https://api.example.com/mcp",
  headers: { Authorization: "Bearer ${GITHUB_TOKEN}" },
  oauth: { client_id: "abc", client_secret: "${SECRET}", callback_port: 8765, scope: "repo" },
  enabled: true,
};

describe("mcp server draft", () => {
  it("round-trips a configuration through the form draft", () => {
    expect(fromDraft(toDraft(remote))).toEqual(remote);
  });

  it("trims names, drops blank rows and empty args, and keeps values as typed", () => {
    const draft = {
      ...emptyDraft(),
      name: "  fs  ",
      command: " uvx ",
      argsText: "server\n\n/tmp\n",
      envRows: [{ name: " KEY ", value: " v " }, { name: "", value: "ignored" }],
    };

    expect(fromDraft(draft)).toMatchObject({ name: "fs", command: "uvx", args: ["server", "/tmp"], env: { KEY: " v " }, oauth: null });
  });

  it("treats an all-empty OAuth block, or any stdio server, as no OAuth", () => {
    expect(fromDraft({ ...toDraft(remote), oauthClientId: "", oauthClientSecret: "", oauthCallbackPort: "", oauthScope: "" }).oauth).toBeNull();
    expect(fromDraft({ ...toDraft(remote), transport: "stdio" }).oauth).toBeNull();
  });
});
