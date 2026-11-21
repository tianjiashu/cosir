# 工具展示数据契约

## 1 归属

`ToolObservation.display_data` 是供界面消费的结构化结果，不是模型正文或执行事实源。呈现类别由 ToolDefinition.conversation_presentation 声明，Transport 在实时与重建时投影；动词、图标、目标与无意图时的标题由前端 tool-catalog 声明，详情按 kind 路由；producer 只投影已有结果，不额外 IO。完整模型结果不通过 UI 字段重复传播。

成功观察可不提供展示数据；提供时必须有稳定 kind。错误/取消由工具结果治理收口为短提示（`status_hint`，≤8 字，由失败的调用点按原因分类给出，如「原文未匹配」「需先读取」「需要登录」）；实际已收集的结构化部分结果可保留 kind（终端取消、超时输出，补丁部分写入后失败时的 `file-changes`），不把部分结果解释为成功。Transport 的 completed 只表示调用完成，pending 可能是后台占位；awaitingUser 与 background 必须读取显式字段。Run 状态不等于工具状态，后台 completed 不等于业务成功。

调用意图不是平台字段：参数一眼看不懂的工具（`execute_terminal`、`browser_evaluate`）在参数模型里用 `tools/tool_models/call_intent.intent_field` 声明 `intent`，其余工具按「动词 · 目标」展示。它是普通业务参数（缺省空串，漏写不拒绝），只服务展示：执行、授权与日志不读它，不发给宿主；前端工具目录按工具名从 `args.intent` 取标题，终端后台标签取它，空时回落到命令首个非空行。原始调用参数是唯一持久来源，模型流收口与冷重建都用 `display_arguments` 解码后投影为 `tool part.args`。

前端 `TurnLayout` 将当前活动放在最近的过程区间，正文开始后前一区间转为历史摘要。模型阶段与后台登记组合展示，后台收尾不冒充运行；渲染与估高共享区间可见规则。普通工具组、时间线终端详情默认折叠；用户选择、必要审批与独立 Reader 保留。

## 2 呈现规则

- `wait_messages` 正常调用是纯控制，默认无活动行、不计工作次数；错误、取消或需要操作仍可见。`browser_tabs` 无 kind 但仍保留通用活动行。
- 工具按原始顺序投影到活动、待决定和结果入口；原调用身份不变，不因折叠新增消息。
- 异常结果由结构化字段解释：命令非零退出、图片失败、浏览器未完成步骤、网页 partial、MCP 服务报错；不靠解析中文提示、模型正文或命令内容判断结果。
- 文件变更是每次操作的记录，不能累加为工作区净 diff，也不保证列全终端或失败工具的副作用。
- 未知 kind 保留通用标题和状态，不能因此隐藏错误或必要操作。

## 3 工具与展示 kind

| 工具 | kind | 说明 |
|---|---|---|
| `read_file` | `read-file-meta` | 结构化结果见第 4 节 |
| `read_image` | `image-read` | 结构化结果见第 4 节 |
| `write_file` | `file-changes` | 结构化结果见第 4 节 |
| `edit_file` | `file-changes` | 结构化结果见第 4 节 |
| `apply_patch` | `file-changes` | 结构化结果见第 4 节 |
| `search_content` | `content-search-results` | 结构化结果见第 4 节 |
| `find_files` | `file-list` | 结构化结果见第 4 节 |
| `execute_terminal` | `terminal-result` | 结构化结果见第 4 节 |
| `terminal_list` | `terminal-session` | 结构化结果见第 4 节 |
| `terminal_read` | `terminal-session` | 结构化结果见第 4 节 |
| `terminal_write` | `terminal-session` | 结构化结果见第 4 节 |
| `terminal_stop` | 无 | 结构化结果见第 4 节 |
| `web_search` | `web-search-results` | 结构化结果见第 4 节 |
| `web_fetch` | `web-fetch-urls` | 结构化结果见第 4 节 |
| `browser_navigate` | `browser-page` | 结构化结果见第 4 节 |
| `browser_snapshot` | `browser-page` | 结构化结果见第 4 节 |
| `browser_act` | `browser-page` | 结构化结果见第 4 节 |
| `browser_screenshot` | `browser-page` | 结构化结果见第 4 节 |
| `browser_logs` | `browser-page` | 结构化结果见第 4 节 |
| `browser_evaluate` | `browser-page` | 结构化结果见第 4 节 |
| `browser_tabs` | 无 | 结构化结果见第 4 节 |
| `wait_messages` | 无 | 结构化结果见第 4 节 |
| `delegate_task` | `delegation-result` | 结构化结果见第 4 节 |
| `fork_session` | `fork-session-result` | 结构化结果见第 4 节 |
| `session_send` | `agent-session-result` | 结构化结果见第 4 节 |
| `session_list` | 无 | 纯行：结果不指向单个 Session |
| `session_read` | `agent-session-result` | 结构化结果见第 4 节 |
| `session_stop` | `agent-session-result` | 结构化结果见第 4 节 |
| `propose_agent_configuration` | `agent-configuration-draft` | 结构化结果见第 4 节 |
| `ask_user` | `user-question` | 结构化结果见第 4 节 |
| `present_deliverables` | `deliverables` | 展示数据只带这批交付的说明；交付记录是结果行 metadata 的 `facts.deliverables`（持久化事实），投影为工具 part 的 `deliverables` 字段（`type`、`key`、`caption`，文件另有 `workspacePath`、`absolutePath`、`kind`、`size`、`width`、`height`，目录有前两者，网址有 `url`），交付卡与产出清单读它 |
| `offer_next_step` | `next-step` | 结构化结果见第 4 节 |
| `skill` | `skill-content` | 结构化结果见第 4 节 |
| `tool_search` | 无 | 只有通用活动行；加载的工具是结果行 metadata 的 `facts.loaded_external_tools` / `facts.loaded_builtin_tools`（持久化事实，不投影到前端） |
| `mcp__<服务>__<工具>` | `mcp-tool-result` | `tool_search` 加载的 MCP 工具：名字不在封闭集合里，前端工具目录按前缀认成一族；结构化结果见第 4 节 |

## 4 字段与解释

下列字段为各 producer 的展示载荷；可选表示该能力或执行阶段可能没有该字段。表不定义执行参数。

| kind | 字段与边界 |
|---|---|
| read-file-meta | path、line_range（start/end，end 可为 END/null）、file_size；可选 truncated/status_hint，不含文件正文 |
| image-read | images[{path,asset_id,width,height}]、failures[{path,hint}]；失败图片数与调用数不同 |
| file-changes | changes[{path,status,patch,insertions,deletions}]（不另带总数，客户端逐项相加）；status 只有 added / modified（删除与移动走终端）；同路径多次操作保持原序，不代表最终文件内容；patch 为空串表示内容没变，为 null 表示超出展示上限（`DISPLAY_PATCH_MAX_CHARS`）未下发 |
| content-search-results | pattern、path、matches[{path,line,content,is_match}]、page{offset,total}（按输出行计，含上下文行）、match_count |
| file-list | files[{path}]、pattern、path、page{offset,total}、match_count；列表条数受 `LIST_RESULT_LIMIT_MAX` 约束 |
| terminal-result | command、workdir、output、end（exited/signaled/timed_out/cancelled/start_failed/worker_failed），exit_code 只在 exited、signal 只在 signaled 时出现；启动时与后台占位只有 command/workdir，不能把缺失 exit_code 当作零；`output` 是原始文本的最近一段（窗口见 `core/tool/terminal_output_window.py`），可选 `output_omitted_lines`（前面省略的行数）与 `output_log_path`（完整输出日志的工作区相对路径，只在超过模型预算时存在） |
| terminal-session | list: terminal_count；read/write: terminal_id/lines/screen/wait_result，可含 input_summary（write 为填入的命令摘要） |
| web-search-results | query、results[{title,url}]，不含网页正文 |
| web-fetch-urls | partial（必有 boolean）、urls[{url}]；URL 可能包含失败页面，URL 数不是成功页数；全部失败走错误观察，不出现此 kind |
| browser-page | tab_id/url/title；可含 screenshot（asset ID）、steps[{action,target,status,hint}]；步骤 status 为 done/failed/skipped/unknown；done 只表示输入执行完成，unknown 表示可能生效、须观察后决定，skipped 不算执行失败 |
| delegation-result | title、child_agent_id；可含 role/delegation_id/child_session_id/writable_root（子会话被收窄时的可写根，相对工作区根） |
| fork-session-result | title、child_session_id；可含 writable_root（同上） |
| agent-session-result | send / stop: operation/session_id/agent_name；read 另带 final_output/end_reason（过程只给模型，不进展示数据）；当前 Session 状态由既有 Session 读取路径提供 |
| agent-configuration-draft | status=draft、agent_id、role、description、system_prompt；保存和编辑属于 Workbench，遵守调用方只读限制 |
| user-question | questions[{question,header,options[{label,description,preview?}],multi_select}]（参数原样）；作答后补 decision（approve / reject）、answers{问题原文: label 或自由文字，多选为列表}、annotations{问题原文: {notes}}；全部跳过只有 decision=reject；作废（未作答）不补字段，状态为 cancelled |
| deliverables | note（这批交付的说明，去空白后为空即 null，长度上限在参数模型）：卡片顶部的一两句话，按纯文本显示；一次调用一张卡，交付项读 part 上的交付记录，不在这里 |
| next-step | ask、replies[1–2]：回复下方的一个问题与按钮，第一个是推荐；不是等待中的决定，后端不登记，点按钮即替用户发出标签原文；前端只在会话最后一条正常完成的回复上显示，同一回合只认最后一次 |
| mcp-tool-result | server_name/tool_name（服务与原工具名）/scope（system / workspace）/images（结果图片的附件 asset id，卡片显示缩略图） |
| skill-content | action=list: skills[{name,description,scope}]；action=load: skill_name/resource_path/scope，正文只进模型上下文 |

`terminal-result` 的实时预览由命令启动时的初始展示（`terminal_command_started`，命令与工作目录）建立、输出块只往上追加，与终态展示数据使用同一窗口规则（`trim_output_window`）：卡片只保留最近一段，终态展示数据到达后整体覆盖实时预览，前端不自己截断。

分页 page 为 offset/limit/has_more/next_offset。链接须经现有 URL 边界处理；本机图片经附件资源入口读取，不直接拼接本机路径。

## 5 修改与验证

修改 producer 时同步 kind 消费方、共享结果信号、测试和本文。绿地项目不保留旧展示结构兼容分支。`test_tool_registry_display_guard.py` 核对工具目录/本文清单，字段语义由各工具测试与前端对应视图测试验证。实时与冷重建继续复用原工具 metadata，不为展示折叠增加持久业务字段。
