import { describe, expect, it } from "vitest";

import type { MCPServerConfiguration } from "@/lib/api/extensions";
import { argsEditableAsLines, emptyDraft, fromDraft, toDraft, withArgsText } from "@/lib/mcp/server-draft";

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
      ...withArgsText(emptyDraft(), "server\n\n/tmp\n"),
      name: "  fs  ",
      command: " uvx ",
      envRows: [{ name: " KEY ", value: " v " }, { name: "", value: "ignored" }],
    };

    expect(fromDraft(draft)).toMatchObject({ name: "fs", command: "uvx", args: ["server", "/tmp"], env: { KEY: " v " }, oauth: null });
  });

  it("treats an all-empty OAuth block, or any stdio server, as no OAuth", () => {
    expect(fromDraft({ ...toDraft(remote), oauthClientId: "", oauthClientSecret: "", oauthCallbackPort: "", oauthScope: "" }).oauth).toBeNull();
    expect(fromDraft({ ...toDraft(remote), transport: "stdio" }).oauth).toBeNull();
  });

  it("参数数组是事实：含空串或换行的参数不能按行编辑，只改别处时原样保存", () => {
    const local: MCPServerConfiguration = { ...remote, transport: "stdio", command: "demo", url: "", headers: {}, oauth: null, args: ["--label", "", "line1\nline2"] };
    const draft = toDraft(local);
    expect(argsEditableAsLines(draft.args)).toBe(false);
    expect(fromDraft({ ...draft, name: "renamed" }).args).toEqual(["--label", "", "line1\nline2"]);
    expect(argsEditableAsLines(toDraft({ ...local, args: ["--label", "x"] }).args)).toBe(true);
  });

  it("在文本框里改参数才按行重新解析", () => {
    const draft = withArgsText(toDraft({ ...remote, transport: "stdio", command: "demo", url: "", headers: {}, oauth: null, args: ["a"] }), "a\nb\n");
    expect(draft.argsText).toBe("a\nb\n");
    expect(fromDraft(draft).args).toEqual(["a", "b"]);
  });

  it("只提交当前传输的字段；另一种传输的值留在草稿里，切回去还在", () => {
    const local: MCPServerConfiguration = { ...remote, transport: "stdio", command: "demo", args: ["--label", "${UNSET}"], env: { KEY: "v" }, url: "", headers: {}, oauth: null };
    const draft = { ...toDraft(local), transport: "streamable_http" as const, url: "https://example.com/mcp", headerRows: [{ name: "X", value: "1" }] };
    expect(fromDraft(draft)).toMatchObject({ transport: "streamable_http", command: "", args: [], env: {}, url: "https://example.com/mcp", headers: { X: "1" } });
    expect(fromDraft({ ...draft, transport: "stdio" })).toMatchObject({ command: "demo", args: ["--label", "${UNSET}"], env: { KEY: "v" }, url: "", headers: {}, oauth: null });
  });
});
