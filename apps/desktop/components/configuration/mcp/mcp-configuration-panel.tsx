import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { InfoIcon, NetworkIcon, PlusIcon, WrenchIcon } from "lucide-react";

import { ConfigListSection } from "@/components/configuration/config-list-section";
import { MCPServerEditor } from "@/components/configuration/mcp/mcp-server-editor";
import { MCPServerRow } from "@/components/configuration/mcp/mcp-server-row";
import { useMCPServerTests } from "@/components/configuration/mcp/use-mcp-server-tests";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/state-panel";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  createMCPServer,
  deleteMCPServer,
  replaceMCPServer,
  setMCPServerEnabled,
  type MCPServer,
  type MCPServerConfiguration,
} from "@/lib/api/extensions";
import { scopeOf } from "@/lib/api/configuration-scope";
import { confirmDelete } from "@/lib/confirm/confirm-store";
import { notify } from "@/lib/notify";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { invalidateMcpServers } from "@/lib/query/extension-invalidation";
import { queryKeys } from "@/lib/query/query-keys";
import { mcpServersQueryOptions } from "@/lib/query/query-options";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";

// 测试进展的键：已保存的服务按名称，编辑框里的草稿单独一个（与同名的已保存服务互不覆盖）。
const DRAFT_KEY = "draft";
const serverKey = (name: string) => `server:${name}`;

/**
 * 一个作用域的 MCP 服务连接配置、工具发现与启停管理。`workspaceId` 为 `null` 时管理系统级服务；
 * 否则管理该工作区的 `.cosir/config/mcp_servers.json`，并列出继承来的系统级服务（只读）。
 */
export function MCPConfigurationPanel({ workspaceId }: { workspaceId: number | null }) {
  const queryClient = useQueryClient();
  const scope = scopeOf(workspaceId);
  // 正在编辑的服务：`null` 是新增，`undefined` 是弹窗关着；`session` 换 key 让每次打开都是一份新草稿。
  const [editing, setEditing] = useState<{ server: MCPServer | null; session: number } | undefined>();
  const { controller: tests, states: testStates } = useMCPServerTests(workspaceId);

  const serversQuery = useQuery(mcpServersQueryOptions(workspaceId));
  const allServers = serversQuery.data?.servers ?? [];
  // 本作用域自己的服务：只有它们能在这里编辑、启停与删除，继承来的系统级服务只读。
  const ownServers = allServers.filter((server) => server.scope === scope);
  const inheritedServers = allServers.filter((server) => server.scope !== scope);

  // 写入按服务寻址、只带这一项的意图（后端基于最新文件合并，页面列表过期也不会覆盖别的服务）。每次写入返回完整
  // 列表直接落进缓存，所以同一时刻只发一次（按钮在忙时禁用）：并发时响应先后不定，旧列表会盖住新列表。
  const saveMutation = useMutation({
    mutationFn: (write: () => Promise<{ servers: MCPServer[] }>) => write(),
    onSuccess: (saved) => {
      queryClient.setQueryData(queryKeys.mcpServers(workspaceId), saved);
      if (workspaceId === null) void invalidateMcpServers(queryClient, null);
    },
  });
  const busy = saveMutation.isPending;

  // 本作用域里已启用的服务；「全部测试」与改变量后的重测只测它们（继承来的系统级服务在系统配置里测）。
  const testable = ownServers.filter((server) => server.enabled);

  function testServers(items: MCPServerConfiguration[]) {
    void tests.testAll(items.map((server) => ({ key: serverKey(server.name), config: server })));
  }

  // 「测试通过」认定义、不认变量值：改了变量就重测引用它的服务，否则卡片还绿着、Agent 已经调不通。
  function retestServersUsing(names: string[]) {
    const using = new Set(ownServers
      .filter((server) => server.variables.some((variable) => names.includes(variable.name)))
      .map((server) => server.name));
    testServers(testable.filter((server) => using.has(server.name)));
  }

  // 测试控制器常驻（离开页面不丢进行中的授权）；只有编辑框草稿那一路随面板卸载结束——草稿本身随之消失。
  useEffect(() => () => tests.clear(DRAFT_KEY), [tests]);

  // 打开和关闭编辑框都丢掉草稿的测试进展：上一份草稿的结论不带进新草稿，进行中的授权随之结束。
  function openEditor(server: MCPServer | null) {
    tests.clear(DRAFT_KEY);
    setEditing((current) => ({ server, session: (current?.session ?? 0) + 1 }));
  }

  function closeEditor() {
    tests.clear(DRAFT_KEY);
    setEditing(undefined);
  }

  /** 编辑框保存：编辑已有服务即替换它（可改名），否则新增；失败抛给编辑框显示。 */
  async function saveServer(config: MCPServerConfiguration) {
    const editingName = editing?.server?.name;
    await saveMutation.mutateAsync(() => (editingName
      ? replaceMCPServer(workspaceId, editingName, config)
      : createMCPServer(workspaceId, config)));
    closeEditor();
    notify.success("MCP 配置已保存");
    // 保存即测：不交互的测试不会弹出任何东西，用户马上知道 Agent 能不能用。
    if (config.enabled) void tests.test(serverKey(config.name), config);
  }

  function toggleServer(server: MCPServer) {
    saveMutation.mutate(() => setMCPServerEnabled(workspaceId, server.name, !server.enabled), {
      onSuccess: () => notify.success(`已${server.enabled ? "停用" : "启用"} ${server.name}`),
      onError: (error) => notify.error(`${server.enabled ? "停用" : "启用"} ${server.name} 失败`, { error, fallback: "保存 MCP 配置失败" }),
    });
  }

  async function deleteServer(server: MCPServer) {
    const deleted = await confirmDelete({
      target: `MCP 服务「${server.name}」`,
      consequence: server.overrides
        ? "删除后本工作区改用系统级同名服务。"
        : "删除后 Agent 将无法再调用它提供的工具。",
      action: () => saveMutation.mutateAsync(() => deleteMCPServer(workspaceId, server.name)),
      failureFallback: "删除 MCP 服务失败",
    });
    if (deleted) notify.success(`已删除 MCP 服务 ${server.name}`);
  }

  const renderRows = (items: MCPServer[], inherited: boolean) => (
    <ul>
      {items.map((server) => {
        const key = serverKey(server.name);
        return (
          <MCPServerRow
            key={`${server.scope}:${server.name}`}
            server={server}
            inherited={inherited}
            testState={inherited ? undefined : testStates[key]}
            busy={busy}
            onEdit={() => openEditor(server)}
            onTest={() => void tests.test(key, server)}
            onAuthorize={() => void tests.authorize(key, server)}
            onCancelAuthorization={() => tests.cancel(key)}
            onToggle={() => toggleServer(server)}
            onDelete={() => void deleteServer(server)}
            onVariablesSaved={retestServersUsing}
          />
        );
      })}
    </ul>
  );

  return (
    <section className="space-y-5" aria-labelledby="mcp-configuration-heading">
      <header className="flex flex-wrap items-start justify-between gap-3">
        {/* 作用域说明只在悬停时出现：页头只留标题与操作。 */}
        <div className="flex min-w-0 flex-1 items-center gap-1.5">
          <h2 id="mcp-configuration-heading" className="text-lg font-semibold">MCP 服务</h2>
          <Tooltip>
            <TooltipTrigger aria-label="MCP 服务说明" className="text-muted-foreground hover:text-foreground focus-ring rounded-sm"><InfoIcon className="size-3.5" /></TooltipTrigger>
            <TooltipContent className="max-w-80 flex-col items-start gap-1">
              {workspaceId === null ? (
                <span>所有工作区共用；测试时会启动服务并读取它提供的工具。</span>
              ) : (
                <>
                  <span>只在本工作区可用，stdio 服务在工作区根目录运行；与系统级同名时本工作区的优先。</span>
                  <span>配置文件在工作区的 .cosir/config/mcp_servers.json，可能随代码提交，Agent 也能读到；令牌等凭据请写成 {"${变量名}"}，值在本机填写。</span>
                </>
              )}
            </TooltipContent>
          </Tooltip>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <TooltipIconButton tooltip="全部测试" className="size-7" onClick={() => testServers(testable)} disabled={testable.length === 0}><WrenchIcon /></TooltipIconButton>
          <TooltipIconButton tooltip="添加服务" className="size-7" onClick={() => openEditor(null)} disabled={!serversQuery.data || busy}><PlusIcon /></TooltipIconButton>
        </div>
      </header>

      {serversQuery.isPending ? (
        <LoadingState label="正在读取 MCP 服务…" />
      ) : serversQuery.isError ? (
        <ErrorState
          message={userErrorMessage(serversQuery.error, "读取 MCP 配置失败")}
          onRetry={() => void serversQuery.refetch()}
          retryLabel="重新读取"
        />
      ) : allServers.length === 0 ? (
        <EmptyState
          icon={<NetworkIcon />}
          title="还没有 MCP 服务"
          description="添加服务并测试通过后，Agent 就可以发现并调用它提供的工具。"
        />
      ) : (
        // 工作区页先列本工作区、再列继承来的系统级（只读）；同名时本工作区的在前，覆盖关系由顺序与徽标表达。
        <div className="space-y-5">
          <ConfigListSection
            title={workspaceId === null ? "系统级" : "本工作区"}
            count={ownServers.length}
            info={workspaceId === null ? "所有工作区都能用；工作区可以另加自己的，同名时工作区的优先。" : "只有这个工作区的 Agent 能用。"}
          >
            {ownServers.length > 0
              ? renderRows(ownServers, false)
              : <p className="text-muted-foreground px-2 py-1.5 text-xs">还没有本工作区的服务，点右上角「添加服务」。</p>}
          </ConfigListSection>
          {inheritedServers.length > 0 && (
            <ConfigListSection title="系统级" count={inheritedServers.length} info="所有工作区共用；在系统配置里编辑、测试与启停。" readOnly>
              {renderRows(inheritedServers, true)}
            </ConfigListSection>
          )}
        </div>
      )}

      {editing && (
        <MCPServerEditor
          key={editing.session}
          server={editing.server}
          saving={busy}
          testState={testStates[DRAFT_KEY]}
          onTest={(config) => void tests.test(DRAFT_KEY, config)}
          onAuthorize={(config) => void tests.authorize(DRAFT_KEY, config)}
          onCancelAuthorization={() => tests.cancel(DRAFT_KEY)}
          onSave={saveServer}
          onClose={closeEditor}
        />
      )}
    </section>
  );
}
