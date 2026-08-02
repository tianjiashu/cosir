# Web Tools

`web_search` 与 `web_extract` 是后端工具系统的一等公民工具，复刻成熟 coding-agent 的网页能力，但严格集成进本项目的 `ToolDefinition` / `ToolSystem` 架构。两个工具都只暴露给模型，Provider 选择由内部轻量注册表完成，不引入 Hermes 风格的模块级全局工具注册表。

## Tools

`web_search` 搜索网页并返回结果元数据（标题、URL、描述、位置、Provider），不含正文内容。

`web_extract` 读取指定 URL 并返回清理后的页面正文；不做任何 LLM 摘要。它接受 URL 字符串，或包含字符串 `url` / `href` 的搜索结果对象。

## Configuration

通过环境变量配置后端，全部在 `Settings` 加载时从 `CODING_AGENT_*` 前缀读取：

设置一个共享后端（同时用于搜索与提取）：

```env
CODING_AGENT_WEB_BACKEND=tavily
```

或按能力分别设置后端：

```env
CODING_AGENT_WEB_SEARCH_BACKEND=brave-free
CODING_AGENT_WEB_EXTRACT_BACKEND=firecrawl
```

其它可调项：

```env
CODING_AGENT_WEB_REQUEST_TIMEOUT_SECONDS=20
CODING_AGENT_WEB_SEARCH_LIMIT_MAX=20
CODING_AGENT_WEB_EXTRACT_URL_LIMIT_MAX=5
CODING_AGENT_WEB_EXTRACT_CHAR_LIMIT=15000
CODING_AGENT_WEB_EXTRACT_STORE_DIR_NAME=.coding-agent/tool-results/web
```

支持的后端名称（按默认回退优先级排序）：

- `firecrawl`
- `parallel`
- `tavily`
- `exa`
- `searxng`
- `brave-free`
- `ddgs`

选择顺序：`WEB_SEARCH_BACKEND` / `WEB_EXTRACT_BACKEND` 优先于 `WEB_BACKEND`；显式配置的后端即使不可用也会被返回（用于精确报出缺失凭据），否则按上述优先级挑选第一个可用且具备对应能力的 Provider。

## Safety

`web_extract` 在任何 Provider 请求之前，对输入 URL 执行以下校验：

- 拒绝含内嵌密钥的 URL（如 `sk-`、`xox*`、`ghp_*`、API Key / Bearer 等）。
- 拒绝含凭据型查询参数的 URL（如 `?api_key=`、 `?token=`、 `?password=` 等）。
- 拒绝非 `http` / `https` 协议（如 `file://`、 `ftp://`）。
- 解析主机名并拒绝回环、私有、链路本地、组播、保留、未指定及站点本地地址（含 CGNAT `100.64.0.0/10`）。
- 单次调用最多接受 `WEB_EXTRACT_URL_LIMIT_MAX`（默认 5）个 URL。

`web_search` 同样只通过已配置且具备搜索能力的 Provider 发起请求。

## Large Pages

超限的提取正文会在工具响应中被截断（保留头尾各一半预算），完整清理后的内容保存到当前 workspace 内的 `.coding-agent/tool-results/web/` 目录，并附 `read_file` 分页指引，绝不会写到 workspace 根目录之外。

## Formats

`web_extract` 支持 `markdown` / `html` / `text` 三种格式，但会受具体 Provider 能力约束：请求不被支持的格式时，在 Provider 调用前即返回确定性错误。
