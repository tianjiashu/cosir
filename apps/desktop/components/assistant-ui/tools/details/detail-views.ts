import { childAgentView } from "./child-agent-view";
import type { DetailView } from "./detail-view";
import { mcpView, skillView } from "./extension-views";
import { readFileView } from "./read-file-view";
import { webExtractView, webSearchView } from "./web-views";
import { contentSearchView, directoryListView, fileListView } from "./workspace-list-views";

/**
 * 走通用低噪音卡片的 `display_data.kind` 及其投影。
 *
 * 这张表同时是 renderer 注册表里 details 路由承接的 kind 清单：新增一种只需要摘要和
 * 展开列表的工具展示时，在这里加一项即可，不要在 `DetailsTool` 里加分支。
 */
export const DETAIL_VIEWS: Readonly<Record<string, DetailView>> = {
  "read-file-meta": readFileView,
  "file-list": fileListView,
  "content-search-results": contentSearchView,
  "directory-list": directoryListView,
  "web-search-results": webSearchView,
  "web-extract-urls": webExtractView,
  "child-agent-result": childAgentView,
  "mcp-tool-result": mcpView,
  "skill-content": skillView,
};
