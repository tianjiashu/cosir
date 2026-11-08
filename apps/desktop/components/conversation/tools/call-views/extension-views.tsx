import type { CallView } from "../call-view";
import { asRecord, readArray, readRecords } from "../display-data-readers";
import { DETAIL_ROW_CLASS, DetailEmpty, listDetail } from "./detail-parts";

/**
 * `mcp-tool-result`：`list` 列出服务与其工具；`call` 的结果正文只进模型上下文，
 * 卡片只显示调用了哪个服务的哪个工具，因此没有展开内容。
 */
export const mcpView: CallView = (data) => {
  return {
    meta: data.action === "call"
      ? `${String(data.server_name ?? "MCP 服务")} · ${String(data.tool_name ?? "工具")}`
      : `${readArray(data.servers).length} 个服务`,
    detail: data.action === "list" ? {
      render: () => {
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
    } : undefined,
  };
};

/** `skill-content`：`list` 列出可用 Skill；`load` 的正文只进模型上下文，只显示 Skill 名。 */
export const skillView: CallView = (data) => {
  return {
    meta: data.action === "list"
      ? `${readArray(data.skills).length} 个 Skill`
      : String(data.skill_name ?? "Skill"),
    detail: data.action === "list" ? listDetail(data, () => readRecords(data.skills), "没有可用 Skill", (skill, index) => (
      <li key={`${String(skill.name ?? "skill")}-${index}`} className={DETAIL_ROW_CLASS}>
        <span className="shrink-0 font-medium">{String(skill.name ?? "未命名 Skill")}</span>
        {typeof skill.description === "string" && skill.description && <span className="text-muted-foreground truncate">{skill.description}</span>}
      </li>
    )) : undefined,
  };
};
