import { asRecord } from "../types";
import { DetailEmpty, readArray, type DetailView } from "./detail-view";

/**
 * `mcp-tool-result`：`list` 列出服务与其工具；`call` 的结果正文只进模型上下文，
 * 卡片只显示调用了哪个服务的哪个工具，因此没有展开内容。
 */
export const mcpView: DetailView = {
  summary: (data) => data.action === "call"
    ? `${String(data.server_name ?? "MCP 服务")} · ${String(data.tool_name ?? "工具")}`
    : `发现 ${readArray(data.servers).length} 个 MCP 服务`,
  body: (data) => {
    if (data.action !== "list") return null;
    const servers = readArray(data.servers).map(asRecord);
    if (servers.length === 0) return <DetailEmpty>没有已启用的 MCP 服务</DetailEmpty>;
    return (
      <ul className="space-y-3 text-xs">
        {servers.map((server, index) => {
          const tools = readArray(server.tools).map(asRecord);
          return (
            <li key={`${String(server.server_name ?? "server")}-${index}`}>
              <p className="font-medium">{String(server.server_name ?? "MCP 服务")}</p>
              {typeof server.error === "string" && <p className="text-muted-foreground mt-1">{server.error}</p>}
              {tools.length > 0 && (
                <ul className="mt-1 space-y-1 pl-3">
                  {tools.map((tool, toolIndex) => (
                    <li key={`${String(tool.name ?? "tool")}-${toolIndex}`}>
                      <span className="font-mono">{String(tool.name ?? "未命名工具")}</span>
                      {typeof tool.description === "string" && tool.description && <span className="text-muted-foreground ml-2">{tool.description}</span>}
                    </li>
                  ))}
                </ul>
              )}
              {tools.length === 0 && typeof server.error !== "string" && <p className="text-muted-foreground mt-1">没有发现工具</p>}
            </li>
          );
        })}
      </ul>
    );
  },
};

/** `skill-content`：`list` 列出可用 Skill；`load` 的正文只进模型上下文，只显示 Skill 名。 */
export const skillView: DetailView = {
  summary: (data) => data.action === "list"
    ? `发现 ${readArray(data.skills).length} 个 Skill`
    : String(data.skill_name ?? "Skill"),
  body: (data) => {
    if (data.action !== "list") return null;
    const skills = readArray(data.skills).map(asRecord);
    if (skills.length === 0) return <DetailEmpty>没有可用 Skill</DetailEmpty>;
    return (
      <ul className="space-y-1 text-xs">
        {skills.map((skill, index) => (
          <li key={`${String(skill.name ?? "skill")}-${index}`}>
            <span className="font-medium">{String(skill.name ?? "未命名 Skill")}</span>
            {typeof skill.description === "string" && skill.description && <span className="text-muted-foreground ml-2">{skill.description}</span>}
          </li>
        ))}
      </ul>
    );
  },
};
