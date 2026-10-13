"use client";

import { useEffect, useState } from "react";
import {
  Loader2Icon,
  PencilIcon,
  PlusIcon,
  ServerIcon,
  Trash2Icon,
  WrenchIcon,
} from "lucide-react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { formatMcpServerJson, parseMcpServerJson } from "@/components/configuration/mcp-config-json";
import {
  getMCPServers,
  saveMCPServers,
  testMCPServer,
  type MCPServerConfiguration,
  type MCPServerTestResult,
  type MCPTransport,
} from "@/lib/api/system-extensions";

type ServerDraft = Omit<MCPServerConfiguration, "args" | "env"> & {
  argsText: string;
  envRows: Array<{ name: string; value: string }>;
  headerRows: Array<{ name: string; value: string }>;
};

const emptyDraft = (): ServerDraft => ({
  name: "",
  transport: "stdio",
  command: "",
  argsText: "",
  envRows: [],
  url: "",
  headerRows: [],
  headers: {},
  enabled: true,
});

function toDraft(server: MCPServerConfiguration): ServerDraft {
  return {
    name: server.name,
    transport: server.transport,
    command: server.command,
    argsText: server.args.join("\n"),
    envRows: Object.entries(server.env).map(([name, value]) => ({ name, value })),
    url: server.url,
    headerRows: Object.entries(server.headers).map(([name, value]) => ({ name, value })),
    headers: server.headers,
    enabled: server.enabled,
  };
}

function fromDraft(draft: ServerDraft): MCPServerConfiguration {
  return {
    name: draft.name.trim(),
    transport: draft.transport,
    command: draft.command.trim(),
    args: draft.argsText.split(/\r?\n/).filter((argument) => argument.length > 0),
    env: Object.fromEntries(draft.envRows.filter((row) => row.name.trim()).map((row) => [row.name.trim(), row.value])),
    url: draft.url.trim(),
    headers: Object.fromEntries(draft.headerRows.filter((row) => row.name.trim()).map((row) => [row.name.trim(), row.value])),
    enabled: draft.enabled,
  };
}

function transportLabel(transport: MCPTransport): string {
  if (transport === "streamable_http") return "Streamable HTTP";
  return transport === "sse" ? "SSE" : "stdio";
}

function ErrorMessage({ children }: { children: string }) {
  return <div role="alert" className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">{children}</div>;
}

/** 系统 MCP 服务的连接配置、工具发现与启停管理。 */
export function MCPConfigurationPanel() {
  const [servers, setServers] = useState<MCPServerConfiguration[]>([]);
  const [loadState, setLoadState] = useState<"loading" | "ready" | "error">("loading");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingName, setEditingName] = useState<string | null>(null);
  const [draft, setDraft] = useState<ServerDraft>(emptyDraft);
  const [editorMode, setEditorMode] = useState<"form" | "json">("form");
  const [jsonDraft, setJsonDraft] = useState("");
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<MCPServerTestResult | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [lastTests, setLastTests] = useState<Record<string, MCPServerTestResult>>({});

  async function load() {
    setLoadState("loading");
    setLoadError(null);
    try {
      const configuration = await getMCPServers();
      setServers(configuration.servers);
      setLoadState("ready");
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : "读取 MCP 配置失败");
      setLoadState("error");
    }
  }

  useEffect(() => {
    void load();
  }, []);

  function openEditor(server?: MCPServerConfiguration) {
    setEditingName(server?.name ?? null);
    const nextDraft = server ? toDraft(server) : emptyDraft();
    setDraft(nextDraft);
    setJsonDraft(formatMcpServerJson(server ?? fromDraft(nextDraft)));
    setEditorMode("form");
    setTestResult(null);
    setFormError(null);
    setDialogOpen(true);
  }

  async function persist(next: MCPServerConfiguration[]) {
    setSaving(true);
    setNotice(null);
    setLoadError(null);
    try {
      const saved = await saveMCPServers(next);
      setServers(saved.servers);
      setLastTests({});
      setNotice("MCP 配置已保存");
      return true;
    } catch (error) {
      const message = error instanceof Error ? error.message : "保存 MCP 配置失败";
      if (dialogOpen) setFormError(message);
      else setLoadError(message);
      return false;
    } finally {
      setSaving(false);
    }
  }

  async function saveDraft(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      const server = editorMode === "json" ? parseMcpServerJson(jsonDraft) : fromDraft(draft);
      const next = editingName
        ? servers.map((item) => item.name === editingName ? server : item)
        : [...servers, server];
      if (await persist(next)) setDialogOpen(false);
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "MCP 配置无效");
    }
  }

  async function toggleServer(server: MCPServerConfiguration) {
    const next = servers.map((item) => item.name === server.name ? { ...item, enabled: !item.enabled } : item);
    await persist(next);
  }

  async function deleteServer(server: MCPServerConfiguration) {
    if (!window.confirm(`删除 MCP 服务“${server.name}”？`)) return;
    if (!(await persist(servers.filter((item) => item.name !== server.name)))) return;
    setLastTests((current) => {
      const next = { ...current };
      delete next[server.name];
      return next;
    });
  }

  async function runTest(server: MCPServerConfiguration) {
    setTesting(true);
    setTestResult(null);
    setFormError(null);
    try {
      const result = await testMCPServer(server);
      setTestResult(result);
      setLastTests((current) => ({ ...current, [server.name]: result }));
      if (result.agent_available) {
        const configuration = await getMCPServers();
        setServers(configuration.servers);
      }
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "MCP 连接测试失败");
    } finally {
      setTesting(false);
    }
  }

  async function testDraft() {
    let server: MCPServerConfiguration;
    try {
      server = editorMode === "json" ? parseMcpServerJson(jsonDraft) : fromDraft(draft);
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "MCP 配置无效");
      return;
    }
    if (!server.name || (server.transport === "stdio" ? !server.command : !server.url)) {
      setFormError(server.transport === "stdio" ? "请填写服务名称和启动命令" : "请填写服务名称和 HTTP endpoint");
      return;
    }
    await runTest(server);
  }

  function toggleEditorMode() {
    setFormError(null);
    if (editorMode === "form") {
      setJsonDraft(formatMcpServerJson(fromDraft(draft)));
      setEditorMode("json");
      return;
    }
    try {
      setDraft(toDraft(parseMcpServerJson(jsonDraft)));
      setEditorMode("form");
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "MCP 配置无效");
    }
  }

  return (
    <section className="space-y-5" aria-labelledby="mcp-configuration-heading">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1.5">
          <h2 id="mcp-configuration-heading" className="text-lg font-semibold">MCP 服务</h2>
          <p className="text-muted-foreground text-sm">配置本机 MCP 服务。测试时会启动服务并读取它提供的工具。</p>
        </div>
        <Button onClick={() => openEditor()} disabled={loadState !== "ready" || saving}><PlusIcon />添加服务</Button>
      </header>

      {notice && <div role="status" className="text-sm text-emerald-700 dark:text-emerald-400">{notice}</div>}
      {loadError && <ErrorMessage>{loadError}</ErrorMessage>}
      {loadState === "loading" ? (
        <div className="text-muted-foreground flex items-center gap-2 py-10 text-sm"><Loader2Icon className="size-4 animate-spin" />正在读取 MCP 服务…</div>
      ) : loadState === "error" ? (
        <Button variant="outline" onClick={() => void load()}>重新读取</Button>
      ) : servers.length === 0 ? (
        <div className="border-border/70 bg-muted/20 rounded-xl border border-dashed px-5 py-10 text-center">
          <ServerIcon className="text-muted-foreground mx-auto mb-3 size-6" />
          <p className="text-sm font-medium">还没有 MCP 服务</p>
          <p className="text-muted-foreground mt-1 text-xs">添加本机服务后，Agent 就可以发现并调用它提供的工具。</p>
        </div>
      ) : (
        <div className="space-y-3">
          {servers.map((server) => {
            const result = lastTests[server.name];
            return (
              <article key={server.name} className="border-border/70 bg-card/70 rounded-xl border p-4">
                <div className="flex flex-wrap items-start gap-3">
                  <ServerIcon className="text-muted-foreground mt-0.5 size-4 shrink-0" />
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <h3 className="truncate text-sm font-medium">{server.name}</h3>
                      <span className={`rounded-full px-2 py-0.5 text-[11px] ${server.enabled ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground"}`}>
                        {server.enabled ? "已启用" : "已停用"}
                      </span>
                      <span className={`rounded-full px-2 py-0.5 text-[11px] ${server.tested ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400" : "bg-muted text-muted-foreground"}`}>
                        {server.tested ? "测试通过" : "待测试"}
                      </span>
                    </div>
                    <p className="text-muted-foreground mt-1 truncate font-mono text-xs">
                      {server.transport === "stdio" ? `${server.command} ${server.args.join(" ")}` : server.url}
                      <span className="ml-2 font-sans">{transportLabel(server.transport)}</span>
                    </p>
                    {!server.tested && <p className="text-muted-foreground mt-2 text-xs">连接测试成功并获取工具列表后，Agent 才能使用此服务。</p>}
                    {result && <p className={`mt-2 text-xs ${result.success ? "text-emerald-700 dark:text-emerald-400" : "text-destructive"}`}>
                      {result.success ? `本次测试成功 · 发现 ${result.tools.length} 个工具` : `本次测试失败 · ${result.error || "请检查服务配置"}`}
                    </p>}
                    {result?.success && result.tools.length > 0 && (
                      <details className="mt-3 rounded-lg bg-muted/40 px-3 py-2">
                        <summary className="cursor-pointer text-xs font-medium">查看发现的工具</summary>
                        <ul className="mt-2 space-y-2">
                          {result.tools.map((tool) => <li key={tool.name} className="text-xs"><span className="font-mono font-medium">{tool.name}</span>{tool.description && <span className="text-muted-foreground ml-2">{tool.description}</span>}</li>)}
                        </ul>
                      </details>
                    )}
                  </div>
                  <Switch checked={server.enabled} aria-label={`${server.enabled ? "停用" : "启用"} ${server.name}`} disabled={loadState !== "ready" || saving || !server.tested} onClick={() => void toggleServer(server)} />
                </div>
                <div className="mt-4 flex flex-wrap justify-end gap-2">
                  <Button variant="outline" size="sm" onClick={() => openEditor(server)} disabled={loadState !== "ready" || saving}><PencilIcon />编辑</Button>
                  <Button variant="outline" size="sm" onClick={() => void runTest(server)} disabled={loadState !== "ready" || testing}><WrenchIcon />测试连接</Button>
                  <Button variant="ghost" size="sm" onClick={() => void deleteServer(server)} disabled={loadState !== "ready" || saving} aria-label={`删除 ${server.name}`}><Trash2Icon /></Button>
                </div>
              </article>
            );
          })}
        </div>
      )}

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{editingName ? "编辑 MCP 服务" : "添加 MCP 服务"}</DialogTitle>
            <DialogDescription>表单和 JSON 使用同一份服务配置。JSON 采用常见的 mcpServers 结构；enabled 是本应用的启停扩展字段，其他客户端配置项可能不受支持。</DialogDescription>
          </DialogHeader>
          <form onSubmit={(event) => void saveDraft(event)} onChange={() => setTestResult(null)} onClick={() => setTestResult(null)} className="space-y-4">
            <div className="flex justify-end">
              <Button type="button" variant="outline" size="sm" onClick={toggleEditorMode}>
                切换到{editorMode === "form" ? "JSON" : "表单"}
              </Button>
            </div>
            {editorMode === "json" ? (
              <div className="space-y-2">
                <Textarea
                  aria-label="MCP JSON 配置"
                  value={jsonDraft}
                  onChange={(event) => setJsonDraft(event.target.value)}
                  className="min-h-72 font-mono text-xs"
                  spellCheck={false}
                />
                <p className="text-muted-foreground text-xs">每次编辑一个服务。JSON 中的密钥会以明文显示。</p>
              </div>
            ) : <>
            <label className="block space-y-1.5 text-sm"><span>服务名称</span><Input required value={draft.name} onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))} /></label>
            <label className="block space-y-1.5 text-sm"><span>传输方式</span><select className="border-input bg-background h-10 w-full rounded-md border px-3 text-sm" value={draft.transport} onChange={(event) => setDraft((current) => ({ ...current, transport: event.target.value as MCPTransport }))}><option value="stdio">stdio（本机进程）</option><option value="sse">SSE</option><option value="streamable_http">Streamable HTTP</option></select></label>
            {draft.transport === "stdio" ? <>
              <label className="block space-y-1.5 text-sm"><span>启动命令</span><Input required value={draft.command} onChange={(event) => setDraft((current) => ({ ...current, command: event.target.value }))} placeholder="例如：uvx" /></label>
              <label className="block space-y-1.5 text-sm"><span>命令参数</span><Textarea value={draft.argsText} onChange={(event) => setDraft((current) => ({ ...current, argsText: event.target.value }))} placeholder="每行一个参数" className="min-h-20 font-mono text-xs" /></label>
              <div className="space-y-2">
                <div className="flex items-center justify-between"><span className="text-sm">环境变量</span><Button type="button" variant="outline" size="sm" onClick={() => setDraft((current) => ({ ...current, envRows: [...current.envRows, { name: "", value: "" }] }))}><PlusIcon />添加变量</Button></div>
                {draft.envRows.map((row, index) => <div key={index} className="grid gap-2 sm:grid-cols-[1fr_1.5fr_auto]"><Input aria-label="环境变量名称" value={row.name} placeholder="名称" onChange={(event) => setDraft((current) => ({ ...current, envRows: current.envRows.map((item, itemIndex) => itemIndex === index ? { ...item, name: event.target.value } : item) }))} /><Input aria-label="环境变量值" type="password" value={row.value} placeholder="值" onChange={(event) => setDraft((current) => ({ ...current, envRows: current.envRows.map((item, itemIndex) => itemIndex === index ? { ...item, value: event.target.value } : item) }))} /><Button type="button" variant="ghost" size="icon" aria-label="移除环境变量" onClick={() => setDraft((current) => ({ ...current, envRows: current.envRows.filter((_, itemIndex) => itemIndex !== index) }))}><Trash2Icon /></Button></div>)}
              </div>
            </> : <>
              <label className="block space-y-1.5 text-sm"><span>HTTP endpoint</span><Input required type="url" value={draft.url} onChange={(event) => setDraft((current) => ({ ...current, url: event.target.value }))} placeholder="https://example.com/mcp" /></label>
              <div className="space-y-2">
                <div className="flex items-center justify-between"><span className="text-sm">HTTP headers（可选）</span><Button type="button" variant="outline" size="sm" onClick={() => setDraft((current) => ({ ...current, headerRows: [...current.headerRows, { name: "", value: "" }] }))}><PlusIcon />添加 header</Button></div>
                {draft.headerRows.map((row, index) => <div key={index} className="grid gap-2 sm:grid-cols-[1fr_1.5fr_auto]"><Input aria-label="HTTP header 名称" value={row.name} placeholder="名称" onChange={(event) => setDraft((current) => ({ ...current, headerRows: current.headerRows.map((item, itemIndex) => itemIndex === index ? { ...item, name: event.target.value } : item) }))} /><Input aria-label="HTTP header 值" type="password" value={row.value} placeholder="值" onChange={(event) => setDraft((current) => ({ ...current, headerRows: current.headerRows.map((item, itemIndex) => itemIndex === index ? { ...item, value: event.target.value } : item) }))} /><Button type="button" variant="ghost" size="icon" aria-label="移除 HTTP header" onClick={() => setDraft((current) => ({ ...current, headerRows: current.headerRows.filter((_, itemIndex) => itemIndex !== index) }))}><Trash2Icon /></Button></div>)}
              </div>
            </>}
            </>}
            {editorMode === "form" && <label className="flex items-center justify-between rounded-lg border px-3 py-2.5 text-sm"><span>启用此服务</span><Switch checked={draft.enabled} aria-label="启用此服务" onClick={() => setDraft((current) => ({ ...current, enabled: !current.enabled }))} /></label>}
            {formError && <ErrorMessage>{formError}</ErrorMessage>}
            {testResult && <div role="status" className={`rounded-lg border px-3 py-2 text-sm ${testResult.success ? "border-emerald-500/30 bg-emerald-500/5" : "border-destructive/30 bg-destructive/10"}`}>
              {testResult.success ? `测试成功，已获取 ${testResult.tools.length} 个工具。${testResult.agent_available ? "Agent 已可使用此服务。" : "保存配置后，还需对已保存服务测试一次才能供 Agent 使用。"}` : `测试失败：${testResult.error || "请检查服务配置"}`}
            </div>}
            {testResult?.success && testResult.tools.length > 0 && <ul className="max-h-40 space-y-1 overflow-y-auto rounded-lg bg-muted/40 p-3 text-xs">{testResult.tools.map((tool) => <li key={tool.name}><span className="font-mono font-medium">{tool.name}</span>{tool.description && <span className="text-muted-foreground ml-2">{tool.description}</span>}</li>)}</ul>}
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => void testDraft()} disabled={testing || saving}>{testing ? <Loader2Icon className="animate-spin" /> : <WrenchIcon />}测试连接</Button>
              <Button type="submit" disabled={saving || testing}>{saving ? <Loader2Icon className="animate-spin" /> : null}保存服务</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </section>
  );
}
