/**
 * 查询键的唯一定义处。
 *
 * 按资源分层（数组前缀即失效范围）：mutation 成功后用最小的前缀失效相关查询，
 * 例如保存工作区级 Agent 只失效 `agents(workspaceId)`，而 `all` 用于后端换代后全部重拉。
 * 新增查询必须在这里登记键，禁止在组件里手写字符串数组。
 */
export const queryKeys = {
  all: ["cosir"] as const,
  workbenchAgent: (workspaceId: number, taskId: number, generation: number) =>
    ["cosir", "workbench-agent", workspaceId, taskId, generation] as const,
  files: (id: number) => ["cosir", "files", id] as const,
  browserVisibility: (id: number) => ["cosir", "files", id, "visibility"] as const,
  fileDirectory: (id: number, path: string, revision: string) => ["cosir", "files", id, "directory", path, revision] as const,
  fileInfo: (id: number, path: string) => ["cosir", "files", id, "info", path] as const,
  fileText: (id: number, path: string, version: string, encoding: string, offset: number, tail: boolean, before?: number) => ["cosir", "files", id, "text", path, version, encoding, offset, tail, before] as const,
  fileResource: (id: number, path: string, version: string) => ["cosir", "files", id, "resource", path, version] as const,
  agents: (workspaceId: number | null) => ["cosir", "agents", workspaceId ?? "system"] as const,
  agentTeams: (workspaceId: number | null) => ["cosir", "agent-teams", workspaceId ?? "system"] as const,
  agentTeamRun: (teamRunId: number) => ["cosir", "agent-team-run", teamRunId] as const,
  instruction: (kind: "global" | "main-agent-prompt" | "workspace", workspaceId?: number) =>
    ["cosir", "instruction", kind, workspaceId ?? null] as const,
  environment: () => ["cosir", "environment"] as const,
  terminalDenylist: () => ["cosir", "terminal-denylist"] as const,
  terminalShells: () => ["cosir", "terminal-shells"] as const,
  workspaceFileIgnore: (workspaceId: number) => ["cosir", "workspace-file-ignore", workspaceId] as const,
  mcpServers: () => ["cosir", "mcp-servers"] as const,
  skills: () => ["cosir", "skills"] as const,
  skill: (name: string) => ["cosir", "skill", name] as const,
  toolGroups: () => ["cosir", "tool-groups"] as const,
  modelConfigs: () => ["cosir", "model-configs"] as const,
  contextMessages: (query: unknown) => ["cosir", "context-messages", query] as const,
};
