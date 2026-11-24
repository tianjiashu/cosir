import { useState } from "react";
import { Loader2Icon, PlusIcon, Trash2Icon, WrenchIcon } from "lucide-react";

import { MCPServerStatusLine } from "@/components/configuration/mcp/mcp-server-status";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import type { MCPServerConfiguration, MCPTransport } from "@/lib/api/extensions";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { formatMcpServerJson, parseMcpServerJson, validateMcpServerConfiguration } from "@/lib/mcp/server-config-json";
import { argsEditableAsLines, emptyDraft, fromDraft, toDraft, withArgsText, type KeyValueRow, type ServerDraft } from "@/lib/mcp/server-draft";
import { isTestInProgress, type ServerTestState } from "@/lib/mcp/server-test-controller";
import { MCP_TRANSPORT_LABELS } from "@/lib/mcp/transport-labels";
import { confirmDiscardUnsaved, useUnsavedChanges } from "@/lib/unsaved-changes";

const EDITOR_MODE_OPTIONS = [
  { value: "form", label: "表单" },
  { value: "json", label: "JSON" },
] as const;


/**
 * 添加或编辑一个 MCP 服务的弹窗：表单与 JSON 两种编辑方式、可选 OAuth、草稿的连接测试与登录授权。
 *
 * 每次打开由父级换 key 重新挂载，初值从 `server`（新增时为空）来，不在打开时手动重置一堆状态。
 * 草稿只在本组件里：保存交给 `onSave`（父级拼整表并提交），失败的原因留在弹窗里；未保存的改动以规范 JSON
 * 比较，关闭前确认。草稿的测试进展用父级测试控制器里单独的一个键（`testState`），与同名的已保存服务互不覆盖。
 *
 * 测试结果只对应测过的那份内容：弹窗记下发起测试或授权时的规范 JSON，当前内容与它不同（改了任何一处，
 * 包括增删行、开关、切换编辑方式后改动）时不显示上一次的结论，改回原样又显示。进行中的测试与授权照常显示、
 * 可以取消。不靠「改动了」事件通知清除，所以新增编辑方式不会漏掉。
 */
export function MCPServerEditor({ server, saving, testState, onTest, onAuthorize, onCancelAuthorization, onSave, onClose }: {
  server: MCPServerConfiguration | null;
  saving: boolean;
  testState: ServerTestState | undefined;
  onTest: (config: MCPServerConfiguration) => void;
  onAuthorize: (config: MCPServerConfiguration) => void;
  onCancelAuthorization: () => void;
  /** 保存这一份配置；失败时抛出，弹窗显示原因并保持打开。 */
  onSave: (config: MCPServerConfiguration) => Promise<void>;
  onClose: () => void;
}) {
  const [draft, setDraft] = useState<ServerDraft>(() => (server ? toDraft(server) : emptyDraft()));
  const [initialJson] = useState(() => formatMcpServerJson(fromDraft(draft)));
  const [editorMode, setEditorMode] = useState<"form" | "json">("form");
  const [jsonDraft, setJsonDraft] = useState(initialJson);
  const [formError, setFormError] = useState<string | null>(null);

  // 最近一次测试或授权发起时的规范 JSON。
  const [testedJson, setTestedJson] = useState<string | null>(null);

  const currentJson = editorMode === "json" ? jsonDraft : formatMcpServerJson(fromDraft(draft));
  useUnsavedChanges("MCP 服务编辑", currentJson !== initialJson);
  const shownState = testState?.phase === "done" && testedJson !== normalizedJson(editorMode, currentJson) ? undefined : testState;
  const result = shownState?.phase === "done" ? shownState.result : null;
  const inProgress = isTestInProgress(testState);
  const update = (patch: Partial<ServerDraft>) => setDraft((current) => ({ ...current, ...patch }));
  // 文本框里敲出的参数不会含空串或换行（见 withArgsText），所以只读只来自载入的配置（打开时或从 JSON 切回）。
  const argsLines = argsEditableAsLines(draft.args);

  /** 读出当前草稿并校验必填项；不合法时把原因写进弹窗、返回 null。 */
  function readDraft(): MCPServerConfiguration | null {
    try {
      const config = editorMode === "json" ? parseMcpServerJson(jsonDraft) : fromDraft(draft);
      validateMcpServerConfiguration(config);
      setFormError(null);
      return config;
    } catch (error) {
      setFormError(userErrorMessage(error, "MCP 配置无效"));
      return null;
    }
  }

  /** 以当前草稿发起测试或授权，并记下测的是哪份内容。 */
  function runWithDraft(run: (config: MCPServerConfiguration) => void) {
    const config = readDraft();
    if (!config) return;
    setTestedJson(formatMcpServerJson(config));
    run(config);
  }

  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const config = readDraft();
    if (!config) return;
    try {
      await onSave(config);
    } catch (error) {
      setFormError(userErrorMessage(error, "保存 MCP 配置失败"));
    }
  }

  async function close() {
    if (await confirmDiscardUnsaved()) onClose();
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
    <Dialog open onOpenChange={(open) => { if (!open) void close(); }}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{server ? "编辑 MCP 服务" : "添加 MCP 服务"}</DialogTitle>
        </DialogHeader>
        <form onSubmit={save} className="space-y-4">
          {/* 保存期间只读：草稿只在本组件里，成功回执即关闭弹窗，等待期间的输入没有去处。 */}
          <fieldset disabled={saving} className="m-0 min-w-0 space-y-4 border-0 p-0">
            <div className="flex justify-end">
              <SegmentedControl aria-label="配置编辑模式" value={editorMode} options={EDITOR_MODE_OPTIONS} onValueChange={selectEditorMode} className="w-36" />
            </div>
            {editorMode === "json" ? (
              <Textarea
                aria-label="MCP JSON 配置"
                value={jsonDraft}
                onChange={(event) => setJsonDraft(event.target.value)}
                className="min-h-72 font-mono text-xs"
                spellCheck={false}
              />
            ) : (
              <>
                <label className="block space-y-1.5 text-sm">
                  <span>服务名称</span>
                  <Input required value={draft.name} onChange={(event) => update({ name: event.target.value })} />
                </label>
                <label className="block space-y-1.5 text-sm">
                  <span>传输方式</span>
                  <NativeSelect value={draft.transport} onChange={(event) => update({ transport: event.target.value as MCPTransport })}>
                    <option value="stdio">{MCP_TRANSPORT_LABELS.stdio}（本机进程）</option>
                    <option value="sse">{MCP_TRANSPORT_LABELS.sse}</option>
                    <option value="streamable_http">{MCP_TRANSPORT_LABELS.streamable_http}</option>
                  </NativeSelect>
                </label>
                {draft.transport === "stdio" ? (
                  <>
                    <label className="block space-y-1.5 text-sm">
                      <span>启动命令</span>
                      <Input required value={draft.command} onChange={(event) => update({ command: event.target.value })} placeholder="例如：uvx" />
                    </label>
                    <label className="block space-y-1.5 text-sm">
                      <span>命令参数</span>
                      <Textarea value={draft.argsText} readOnly={!argsLines} onChange={(event) => setDraft((current) => withArgsText(current, event.target.value))} placeholder="每行一个参数" className="min-h-20 font-mono text-xs" />
                      {!argsLines && <span className="text-muted-foreground block text-xs">参数里有空值或换行，按行编辑会改变它们，请切到 JSON 编辑。</span>}
                    </label>
                    <KeyValueRows label="环境变量" itemLabel="环境变量" addLabel="添加变量" rows={draft.envRows} onChange={(envRows) => update({ envRows })} />
                  </>
                ) : (
                  <>
                    <label className="block space-y-1.5 text-sm">
                      <span>HTTP endpoint</span>
                      <Input required inputMode="url" value={draft.url} onChange={(event) => update({ url: event.target.value })} placeholder="https://example.com/mcp" />
                      <span className="text-muted-foreground block text-xs">密钥等值可写成 {"${变量名}"} 或 {"${变量名:-默认值}"}，从本机环境读取。</span>
                    </label>
                    <KeyValueRows label="HTTP headers（可选）" itemLabel="HTTP header" addLabel="添加 header" rows={draft.headerRows} onChange={(headerRows) => update({ headerRows })} />
                    <OAuthFields draft={draft} onChange={update} />
                  </>
                )}
                <label className="flex items-center justify-between rounded-lg border px-3 py-2.5 text-sm">
                  <span>启用此服务</span>
                  <Switch checked={draft.enabled} aria-label="启用此服务" onClick={() => update({ enabled: !draft.enabled })} />
                </label>
              </>
            )}
          </fieldset>
          {formError && <Alert tone="destructive">{formError}</Alert>}
          <MCPServerStatusLine
            status={null}
            testState={shownState}
            onAuthorize={() => runWithDraft(onAuthorize)}
            onCancel={onCancelAuthorization}
          />
          {result?.success && !result.agent_available && <p className="text-muted-foreground text-xs">保存后 Agent 即可使用此服务。</p>}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => runWithDraft(onTest)} disabled={inProgress || saving}>
              {testState?.phase === "testing" ? <Loader2Icon className="animate-spin" /> : <WrenchIcon />}测试连接
            </Button>
            <Button type="submit" disabled={saving}>{saving && <Loader2Icon className="animate-spin" />}保存服务</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** 环境变量与 HTTP 请求头共用的一组「名称 · 值」行；值按密码框显示，常是密钥。 */
/** 当前内容的规范 JSON（与测过的内容比较用）；JSON 方式下写坏了为 null，不对应任何测过的内容。 */
function normalizedJson(editorMode: "form" | "json", currentJson: string): string | null {
  if (editorMode === "form") return currentJson;
  try {
    return formatMcpServerJson(parseMcpServerJson(currentJson));
  } catch {
    return null;
  }
}

function KeyValueRows({ label, itemLabel, addLabel, rows, onChange }: {
  label: string;
  itemLabel: string;
  addLabel: string;
  rows: KeyValueRow[];
  onChange: (rows: KeyValueRow[]) => void;
}) {
  const set = (index: number, patch: Partial<KeyValueRow>) => onChange(rows.map((row, rowIndex) => (rowIndex === index ? { ...row, ...patch } : row)));
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-sm">{label}</span>
        <Button type="button" variant="ghost" size="sm" onClick={() => onChange([...rows, { name: "", value: "" }])}><PlusIcon />{addLabel}</Button>
      </div>
      {rows.map((row, index) => (
        <div key={index} className="grid gap-2 sm:grid-cols-[1fr_1.5fr_auto]">
          <Input aria-label={`${itemLabel} 名称`} value={row.name} placeholder="名称" onChange={(event) => set(index, { name: event.target.value })} />
          <Input aria-label={`${itemLabel} 值`} type="password" value={row.value} placeholder="值" onChange={(event) => set(index, { value: event.target.value })} />
          <Button type="button" variant="ghost" size="icon" aria-label={`移除 ${itemLabel}`} onClick={() => onChange(rows.filter((_, rowIndex) => rowIndex !== index))}><Trash2Icon /></Button>
        </div>
      ))}
    </div>
  );
}

/** 远程服务的可选 OAuth；Client ID、回调端口或 scope 已填写时默认展开（有 secret 必有 Client ID）。 */
function OAuthFields({ draft, onChange }: { draft: ServerDraft; onChange: (patch: Partial<ServerDraft>) => void }) {
  return (
    <details className="rounded-lg border px-3 py-2" open={Boolean(draft.oauthClientId || draft.oauthCallbackPort || draft.oauthScope)}>
      <summary className="cursor-pointer text-sm">OAuth（可选）</summary>
      <p className="text-muted-foreground mt-2 text-xs">服务需要登录时，点「登录授权」在浏览器里完成授权（测试连接不会打开浏览器），一般无需填写。服务不支持自动注册（如 GitHub）时，填写预先注册的 Client ID，并在服务方把回调地址登记为 http://127.0.0.1:&lt;回调端口&gt;/callback；请求头已带 Authorization 时不走 OAuth。</p>
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <label className="block space-y-1.5 text-sm"><span>Client ID</span><Input value={draft.oauthClientId} onChange={(event) => onChange({ oauthClientId: event.target.value })} /></label>
        <label className="block space-y-1.5 text-sm"><span>Client Secret</span><Input value={draft.oauthClientSecret} placeholder="${变量名}" onChange={(event) => onChange({ oauthClientSecret: event.target.value })} /></label>
        <label className="block space-y-1.5 text-sm"><span>回调端口</span><Input inputMode="numeric" value={draft.oauthCallbackPort} placeholder="随机" onChange={(event) => onChange({ oauthCallbackPort: event.target.value })} /></label>
        <label className="block space-y-1.5 text-sm"><span>Scope</span><Input value={draft.oauthScope} placeholder="服务未声明时使用" onChange={(event) => onChange({ oauthScope: event.target.value })} /></label>
      </div>
    </details>
  );
}
