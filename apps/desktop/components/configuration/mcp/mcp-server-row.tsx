import { Loader2Icon, PencilIcon, Trash2Icon, WrenchIcon } from "lucide-react";

import { MCPServerStatusLine } from "@/components/configuration/mcp/mcp-server-status";
import { MCPServerVariables } from "@/components/configuration/mcp/mcp-server-variables";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import type { MCPServer } from "@/lib/api/extensions";
import { isTestInProgress, type ServerTestState } from "@/lib/mcp/server-test-controller";
import { MCP_TRANSPORT_LABELS } from "@/lib/mcp/transport-labels";
import { cn } from "@/lib/utils";

// 只在这一行里用的悬停分组（见 `components/ui/reveal-on-hover.ts` 的约定），键盘聚焦与触屏时也显示。
const REVEAL_ON_ROW_HOVER = "opacity-0 group-hover/mcp-row:opacity-100 group-has-[:focus-visible]/mcp-row:opacity-100 [@media(hover:none)]:opacity-100";

/**
 * MCP 服务清单里的一行：名称 · 连接方式 · 小号开关；编辑、测试、删除悬停才出现（点名称也能编辑）。
 * 要用户动手的事——填变量、明文密钥提醒、待测试 / 需要授权、测试进展与结果——留在行下面直接显示。
 *
 * `inherited` 是工作区里继承来的系统级服务：只读，编辑、测试与启停都在系统配置里做。每行的测试互不阻塞，
 * 只有自己在测试或授权时测试按钮不可点、并常显；写配置（保存、启停、删除）整页共用一个忙碌状态 `busy`。
 */
export function MCPServerRow({
  server,
  inherited,
  testState,
  busy,
  onEdit,
  onTest,
  onAuthorize,
  onCancelAuthorization,
  onToggle,
  onDelete,
  onVariablesSaved,
}: {
  server: MCPServer;
  inherited: boolean;
  testState: ServerTestState | undefined;
  busy: boolean;
  onEdit: () => void;
  onTest: () => void;
  onAuthorize: () => void;
  onCancelAuthorization: () => void;
  onToggle: () => void;
  onDelete: () => void;
  onVariablesSaved: (names: string[]) => void;
}) {
  const inProgress = isTestInProgress(testState);
  const target = server.transport === "stdio" ? `${server.command} ${server.args.join(" ")}` : server.url;
  return (
    <li className={cn("group/mcp-row hover:bg-muted/50 rounded-md px-2 py-1.5", !server.enabled && "text-muted-foreground")}>
      <div className="flex min-w-0 items-center gap-3">
        {inherited ? (
          <span className="shrink-0 truncate text-sm">{server.name}</span>
        ) : (
          <button type="button" onClick={onEdit} disabled={busy} className={cn("focus-ring shrink-0 truncate rounded-sm text-sm hover:underline", server.enabled && "text-foreground font-medium")}>{server.name}</button>
        )}
        <span className="text-muted-foreground min-w-0 flex-1 truncate font-mono text-xs" title={target}>
          {target}<span className="ml-2 font-sans">{MCP_TRANSPORT_LABELS[server.transport]}</span>
        </span>
        {server.overrides && <Badge size="sm" tone="neutral" title="系统级也有同名服务，在这个工作区里用的是这一份">覆盖系统同名</Badge>}
        {inherited ? (
          <span className="text-muted-foreground shrink-0 text-xs">{server.enabled ? "已启用" : "已停用"}</span>
        ) : (
          <>
            <div className="flex shrink-0 items-center">
              <Button variant="ghost" size="icon-xs" aria-label={`测试 ${server.name}`} title="测试连接" onClick={onTest} disabled={inProgress} className={inProgress ? undefined : REVEAL_ON_ROW_HOVER}>
                {testState?.phase === "testing" ? <Loader2Icon className="animate-spin" /> : <WrenchIcon />}
              </Button>
              <Button variant="ghost" size="icon-xs" aria-label={`编辑 ${server.name}`} title="编辑" onClick={onEdit} disabled={busy} className={REVEAL_ON_ROW_HOVER}><PencilIcon /></Button>
              <Button variant="ghost" size="icon-xs" aria-label={`删除 ${server.name}`} title="删除" onClick={onDelete} disabled={busy} className={REVEAL_ON_ROW_HOVER}><Trash2Icon /></Button>
            </div>
            <Switch size="sm" checked={server.enabled} aria-label={`${server.enabled ? "停用" : "启用"} ${server.name}`} disabled={busy} onClick={onToggle} />
          </>
        )}
      </div>
      <MCPServerVariables server={server} onSaved={onVariablesSaved} />
      {server.literal_fields.length > 0 && (
        <Alert tone="warning" className="mt-1 text-xs">
          {server.literal_fields.join("、")} 直接写了值。工作区配置可能随代码提交，若是密钥请改成 {"${变量名}"}。
        </Alert>
      )}
      <MCPServerStatusLine
        status={server.status}
        testState={testState}
        readOnly={inherited}
        onAuthorize={onAuthorize}
        onCancel={onCancelAuthorization}
      />
    </li>
  );
}
