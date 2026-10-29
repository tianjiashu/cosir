"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/state-panel";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { formatMcpServerJson, parseMcpServerJson, validateMcpServerConfiguration } from "@/components/configuration/mcp-config-json";
import {
  getMCPServers,
  saveMCPServers,
  testMCPServer,
  type MCPServerConfiguration,
  type MCPServerTestResult,
  type MCPTransport,
} from "@/lib/api/system-extensions";
import { confirmDelete } from "@/lib/confirm/confirm-store";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";
import { confirmDiscardUnsaved, useUnsavedChanges } from "@/lib/unsaved-changes";

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

const EDITOR_MODE_OPTIONS = [
  { value: "form", label: "表单" },
  { value: "json", label: "JSON" },
] as const;

/** 系统 MCP 服务的连接配置、工具发现与启停管理。 */
export function MCPConfigurationPanel() {
  const queryClient = useQueryClient();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingName, setEditingName] = useState<string | null>(null);
  const [draft, setDraft] = useState<ServerDraft>(emptyDraft);
  const [editorMode, setEditorMode] = useState<"form" | "json">("form");
  const [jsonDraft, setJsonDraft] = useState("");
  // 打开编辑器时的规范化 JSON；与当前草稿比较得到未保存状态，两种编辑方式用同一口径。
  const [initialJson, setInitialJson] = useState("");
  const [testResult, setTestResult] = useState<MCPServerTestResult | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [lastTests, setLastTests] = useState<Record<string, MCPServerTestResult>>({});

  const serversQuery = useQuery({
    queryKey: queryKeys.mcpServers(),
    queryFn: ({ signal }) => getMCPServers({ signal }),
  });
  const servers = serversQuery.data?.servers;

  // 整表 PUT：保存、启停、删除都提交完整列表，因此同一时刻只允许一次写入，避免基于旧列表互相覆盖。
  const saveMutation = useMutation({
    mutationFn: saveMCPServers,
    onSuccess: (saved) => {
      queryClient.setQueryData(queryKeys.mcpServers(), saved);
      // 「本次测试」结果只对应保存前的配置，保存后服务会按新配置重建，旧结果不再可信。
      setLastTests({});
    },
  });

  const testMutation = useMutation({
    mutationFn: testMCPServer,
    onSuccess: (result, server) => {
      setLastTests((current) => ({ ...current, [server.name]: result }));
      // 已保存服务测试通过后后端会把它标记为可供 Agent 使用，需要重读 tested 状态。
      if (result.agent_available) void queryClient.invalidateQueries({ queryKey: queryKeys.mcpServers() });
    },
  });

  const currentJson = editorMode === "json" ? jsonDraft : formatMcpServerJson(fromDraft(draft));
  useUnsavedChanges("settings", "MCP 服务编辑", dialogOpen && currentJson !== initialJson);

  const busy = saveMutation.isPending;

  function openEditor(server?: MCPServerConfiguration) {
    setEditingName(server?.name ?? null);
    const nextDraft = server ? toDraft(server) : emptyDraft();
    const nextJson = formatMcpServerJson(fromDraft(nextDraft));
    setDraft(nextDraft);
    setJsonDraft(nextJson);
    setInitialJson(nextJson);
    setEditorMode("form");
    setTestResult(null);
    setFormError(null);
    setDialogOpen(true);
  }

  async function closeEditor() {
    if (!(await confirmDiscardUnsaved("settings"))) return;
    setDialogOpen(false);
  }

  function readDraft(): MCPServerConfiguration {
    const server = editorMode === "json" ? parseMcpServerJson(jsonDraft) : fromDraft(draft);
    validateMcpServerConfiguration(server);
    return server;
  }

  function saveDraft(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!servers || busy) return;
    let server: MCPServerConfiguration;
    try {
      server = readDraft();
    } catch (error) {
      setFormError(userErrorMessage(error, "MCP 配置无效"));
      return;
    }
    setFormError(null);
    const next = editingName
      ? servers.map((item) => item.name === editingName ? server : item)
      : [...servers, server];
    saveMutation.mutate(next, {
      onSuccess: () => {
        setDialogOpen(false);
        notify.success("MCP 配置已保存");
      },
      onError: (error) => setFormError(userErrorMessage(error, "保存 MCP 配置失败")),
    });
  }

  function toggleServer(server: MCPServerConfiguration) {
    if (!servers || busy) return;
    const next = servers.map((item) => item.name === server.name ? { ...item, enabled: !item.enabled } : item);
    saveMutation.mutate(next, {
      onSuccess: () => notify.success(`已${server.enabled ? "停用" : "启用"} ${server.name}`),
      onError: (error) => notify.error(`${server.enabled ? "停用" : "启用"} ${server.name} 失败`, { error, fallback: "保存 MCP 配置失败" }),
    });
  }

  async function deleteServer(server: MCPServerConfiguration) {
    if (!servers) return;
    const next = servers.filter((item) => item.name !== server.name);
    const deleted = await confirmDelete({
      target: `MCP 服务「${server.name}」`,
      consequence: "删除后 Agent 将无法再调用它提供的工具。",
      action: () => saveMutation.mutateAsync(next),
      failureFallback: "删除 MCP 服务失败",
    });
    if (deleted) notify.success(`已删除 MCP 服务 ${server.name}`);
  }

  function testSavedServer(server: MCPServerConfiguration) {
    testMutation.mutate(server, {
      onError: (error) => notify.error(`测试 ${server.name} 失败`, { error, fallback: "MCP 连接测试失败" }),
    });
  }

  function testDraft() {
    let server: MCPServerConfiguration;
    try {
      server = readDraft();
    } catch (error) {
      setFormError(userErrorMessage(error, "MCP 配置无效"));
      return;
    }
    setTestResult(null);
    setFormError(null);
    testMutation.mutate(server, {
      onSuccess: setTestResult,
      onError: (error) => setFormError(userErrorMessage(error, "MCP 连接测试失败")),
    });
  }

  function selectEditorMode(nextMode: "form" | "json") {
    if (nextMode === editorMode) return;
    setFormError(null);
    if (nextMode === "json") {
      setJsonDraft(formatMcpServerJson(fromDraft(draft)));
      setEditorMode("json");
      return;
    }
    try {
      setDraft(toDraft(parseMcpServerJson(jsonDraft)));
      setEditorMode("form");
    } catch (error) {
      setFormError(userErrorMessage(error, "MCP 配置无效"));
    }
  }

  return (
    <section className="space-y-5" aria-labelledby="mcp-configuration-heading">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1.5">
          <h2 id="mcp-configuration-heading" className="text-lg font-semibold">MCP 服务</h2>
          <p className="text-muted-foreground text-sm">配置本机 MCP 服务。测试时会启动服务并读取它提供的工具。</p>
        </div>
        <Button onClick={() => openEditor()} disabled={!servers || busy}><PlusIcon />添加服务</Button>
      </header>

      {serversQuery.isPending ? (
        <LoadingState label="正在读取 MCP 服务…" />
      ) : serversQuery.isError ? (
        <ErrorState
          message={userErrorMessage(serversQuery.error, "读取 MCP 配置失败")}
          onRetry={() => void serversQuery.refetch()}
          retryLabel="重新读取"
        />
      ) : serversQuery.data.servers.length === 0 ? (
        <EmptyState
          icon={<ServerIcon />}
          title="还没有 MCP 服务"
          description="添加本机服务后，Agent 就可以发现并调用它提供的工具。"
        />
      ) : (
        <div className="space-y-3">
          {serversQuery.data.servers.map((server) => {
            const result = lastTests[server.name];
            return (
              <Card key={server.name} className="p-4">
                <div className="flex flex-wrap items-start gap-3">
                  <ServerIcon className="text-muted-foreground mt-0.5 size-4 shrink-0" />
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <h3 className="truncate text-sm font-medium">{server.name}</h3>
                      <Badge size="sm" tone={server.enabled ? "primary" : "neutral"}>
                        {server.enabled ? "已启用" : "已停用"}
                      </Badge>
                      <Badge size="sm" tone={server.tested ? "success" : "neutral"}>
                        {server.tested ? "测试通过" : "待测试"}
                      </Badge>
                    </div>
                    <p className="text-muted-foreground mt-1 truncate font-mono text-xs">
                      {server.transport === "stdio" ? `${server.command} ${server.args.join(" ")}` : server.url}
                      <span className="ml-2 font-sans">{transportLabel(server.transport)}</span>
                    </p>
                    {!server.tested && <p className="text-muted-foreground mt-2 text-xs">连接测试成功并获取工具列表后，Agent 才能使用此服务。</p>}
                    {result && <p className={`mt-2 text-xs ${result.success ? "text-success" : "text-destructive"}`}>
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
                  <Switch checked={server.enabled} aria-label={`${server.enabled ? "停用" : "启用"} ${server.name}`} disabled={busy || !server.tested} onClick={() => toggleServer(server)} />
                </div>
                <div className="mt-4 flex flex-wrap justify-end gap-2">
                  <Button variant="outline" size="sm" onClick={() => openEditor(server)} disabled={busy}><PencilIcon />编辑</Button>
                  <Button variant="outline" size="sm" onClick={() => testSavedServer(server)} disabled={testMutation.isPending}><WrenchIcon />测试连接</Button>
                  <Button variant="ghost" size="sm" onClick={() => void deleteServer(server)} disabled={busy} aria-label={`删除 ${server.name}`}><Trash2Icon /></Button>
                </div>
              </Card>
            );
          })}
        </div>
      )}

      <Dialog open={dialogOpen} onOpenChange={(open) => { if (!open) void closeEditor(); }}>
        <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{editingName ? "编辑 MCP 服务" : "添加 MCP 服务"}</DialogTitle>
          </DialogHeader>
          <form onSubmit={saveDraft} onChange={() => setTestResult(null)} onClick={() => setTestResult(null)} className="space-y-4">
            <div className="flex justify-end">
              <SegmentedControl
                aria-label="配置编辑模式"
                value={editorMode}
                options={EDITOR_MODE_OPTIONS}
                onValueChange={selectEditorMode}
                className="w-36"
              />
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
              </div>
            ) : <>
            <label className="block space-y-1.5 text-sm"><span>服务名称</span><Input required value={draft.name} onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))} /></label>
            <label className="block space-y-1.5 text-sm"><span>传输方式</span><NativeSelect value={draft.transport} onChange={(event) => setDraft((current) => ({ ...current, transport: event.target.value as MCPTransport }))}><option value="stdio">stdio（本机进程）</option><option value="sse">SSE</option><option value="streamable_http">Streamable HTTP</option></NativeSelect></label>
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
            {formError && <Alert tone="destructive">{formError}</Alert>}
            {testResult && <Alert role="status" tone={testResult.success ? "success" : "destructive"}>
              {testResult.success ? `测试成功，已获取 ${testResult.tools.length} 个工具。${testResult.agent_available ? "Agent 已可使用此服务。" : "保存配置后，还需对已保存服务测试一次才能供 Agent 使用。"}` : `测试失败：${testResult.error || "请检查服务配置"}`}
            </Alert>}
            {testResult?.success && testResult.tools.length > 0 && <ul className="max-h-40 space-y-1 overflow-y-auto rounded-lg bg-muted/40 p-3 text-xs">{testResult.tools.map((tool) => <li key={tool.name}><span className="font-mono font-medium">{tool.name}</span>{tool.description && <span className="text-muted-foreground ml-2">{tool.description}</span>}</li>)}</ul>}
            <DialogFooter>
              <Button type="button" variant="outline" onClick={testDraft} disabled={testMutation.isPending || busy}>{testMutation.isPending ? <Loader2Icon className="animate-spin" /> : <WrenchIcon />}测试连接</Button>
              <Button type="submit" disabled={busy || testMutation.isPending}>{busy ? <Loader2Icon className="animate-spin" /> : null}保存服务</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </section>
  );
}
