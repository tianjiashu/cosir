# 工具展示数据契约

## 1 归属

`ToolObservation.display_data` 是供界面消费的结构化结果，不是模型正文或执行事实源。呈现类别由 ToolDefinition.conversation_presentation 声明，Transport 在实时与重建时投影；动词、图标、目标与无意图时的标题由前端 tool-catalog 声明，详情按 kind 路由；producer 只投影已有结果，不额外 IO。完整模型结果不通过 UI 字段重复传播。

成功观察可不提供展示数据；提供时必须有稳定 kind。错误/取消由工具结果治理收口为短提示；实际已收集的结构化部分结果可保留 kind（终端取消、超时输出），不把部分结果解释为成功。Transport 的 completed 只表示调用完成，pending 可能是后台占位；awaitingUser 与 background 必须读取显式字段。Run 状态不等于工具状态，后台 completed 不等于业务成功。

调用意图由 `ToolDefinition.to_model_tool_definition` 在模型可见 schema 根部统一注入必填 `intent`，不进入业务 Args、Hook 参数、handler kwargs、宿主参数或 MCP 远端参数。执行准入拆出并校验后放入 `ToolExecutionContext.call_intent`；Hook 不得改写该保留字段。终端后台标签消费同一上下文，不再使用工具专有 `description`。

原始 `assistant.tool_calls[].function.arguments.intent` 是唯一持久来源。模型流正常/异常/取消收口都独立发布意图事实，Transport 投影为 `tool part.intent`，不依赖是否准入执行；冷重建使用同一解析规则。不完整 JSON 不猜意图。意图是模型声明的目的，不是进度、执行承诺或授权依据。Task 冻结模型 schema，因此变更后新建 Task 使用新契约，不迁移旧 schema 或保留旧字段兼容分支。

前端 `TurnLayout` 将当前活动放在最近的过程区间，正文开始后前一区间转为历史摘要。模型阶段与后台登记组合展示，后台收尾不冒充运行；渲染与估高共享区间可见规则。普通工具组、时间线终端详情默认折叠；用户选择、必要审批与独立 Reader 保留。

## 2 呈现规则

- `wait_messages` 正常调用是纯控制，默认无活动行、不计工作次数；错误、取消或需要操作仍可见。`browser_tabs` 无 kind 但仍保留通用活动行。
- 工具按原始顺序投影到活动、待决定和结果入口；原调用身份不变，不因折叠新增消息。
- 异常结果由结构化字段解释：命令非零退出、图片失败、浏览器未完成步骤、网页 partial、MCP 服务错误；不靠解析中文提示、模型正文或命令内容判断结果。
- 文件变更是每次操作的记录，不能累加为工作区净 diff，也不保证列全终端或失败工具的副作用。
- 未知 kind 保留通用标题和状态，不能因此隐藏错误或必要操作。

## 3 工具与展示 kind

| 工具 | kind | 说明 |
|---|---|---|
| `read_file` | `read-file-meta` | 结构化结果见第 4 节 |
| `read_image` | `image-read` | 结构化结果见第 4 节 |
| `write_file` | `file-changes` | 结构化结果见第 4 节 |
| `patch_write` | `file-changes` | 结构化结果见第 4 节 |
| `apply_patch` | `file-changes` | 结构化结果见第 4 节 |
| `delete_file` | `file-changes` | 结构化结果见第 4 节 |
| `move_file` | `file-changes` | 结构化结果见第 4 节 |
| `search_content` | `content-search-results` | 结构化结果见第 4 节 |
| `find_files` | `file-list` | 结构化结果见第 4 节 |
| `list_directory` | `directory-list` | 结构化结果见第 4 节 |
| `execute_terminal` | `terminal-result` | 结构化结果见第 4 节 |
| `terminal_list` | `terminal-session` | 结构化结果见第 4 节 |
| `terminal_start` | `terminal-session` | 结构化结果见第 4 节 |
| `terminal_read` | `terminal-session` | 结构化结果见第 4 节 |
| `terminal_write` | `terminal-session` | 结构化结果见第 4 节 |
| `terminal_handoff` | `terminal-session` | 结构化结果见第 4 节 |
| `terminal_close` | `terminal-session` | 结构化结果见第 4 节 |
| `web_search` | `web-search-results` | 结构化结果见第 4 节 |
| `web_extract` | `web-extract-urls` | 结构化结果见第 4 节 |
| `browser_navigate` | `browser-page` | 结构化结果见第 4 节 |
| `browser_snapshot` | `browser-page` | 结构化结果见第 4 节 |
| `browser_act` | `browser-page` | 结构化结果见第 4 节 |
| `browser_screenshot` | `browser-page` | 结构化结果见第 4 节 |
| `browser_logs` | `browser-page` | 结构化结果见第 4 节 |
| `browser_evaluate` | `browser-page` | 结构化结果见第 4 节 |
| `browser_handoff` | `browser-page` | 结构化结果见第 4 节 |
| `browser_tabs` | 无 | 结构化结果见第 4 节 |
| `wait_messages` | 无 | 结构化结果见第 4 节 |
| `delegate_task` | `delegation-result` | 结构化结果见第 4 节 |
| `fork_task` | `fork-task-result` | 结构化结果见第 4 节 |
| `send_message` | `agent-task-result` | 结构化结果见第 4 节 |
| `task_status` | `agent-task-result` | 结构化结果见第 4 节 |
| `propose_agent_configuration` | `agent-configuration-draft` | 结构化结果见第 4 节 |
| `propose_agent_team_configuration` | `agent-team-configuration-draft` | 结构化结果见第 4 节 |
| `agent_team` | `agent-team-approval` | 结构化结果见第 4 节 |
| `mcp` | `mcp-tool-result` | 结构化结果见第 4 节 |
| `skill` | `skill-content` | 结构化结果见第 4 节 |

## 4 字段与解释

下列字段为各 producer 的展示载荷；可选表示该能力或执行阶段可能没有该字段。表不定义执行参数。

| kind | 字段与边界 |
|---|---|
| read-file-meta | path、line_range（start/end，end 可为 END/null）、file_size；可选 truncated/status_hint，不含文件正文 |
| image-read | images[{path,asset_id,width,height}]、failures[{path,hint}]；失败图片数与调用数不同 |
| file-changes | changes[{path,new_path,status,patch,insertions,deletions}]、diff_stats；同路径多次操作保持原序，patch 可为空，不代表最终文件内容 |
| content-search-results | pattern、path、matches[{path,line,content,is_match}]、page、total_rows、match_count、scanned_files、skipped_files |
| file-list | files[{path}]、pattern、path、page、match_count |
| directory-list | path、entries、page、total_entries |
| terminal-result | command、workdir、output、end（exited/signaled/timed_out/cancelled/start_failed/worker_failed），exit_code 只在 exited、signal 只在 signaled 时出现；后台占位只有 command/workdir，不能把缺失 exit_code 当作零 |
| terminal-session | list: session_count；start/close: session_id/status/exit_code（start 可含 initial_cwd/shell_kind/label）；read/write/handoff: session_id/status/lines/screen/wait_result，可含 input_summary |
| web-search-results | query、results[{title,url}]，不含网页正文 |
| web-extract-urls | partial（必有 boolean）、urls[{url}]；URL 可能包含失败页面，URL 数不是成功页数；全部失败走错误观察，不出现此 kind |
| browser-page | tab_id/url/title；可含 screenshot（asset ID）、handoff_reason、steps[{action,target,status,hint}]；步骤 status 为 done/failed/skipped/unknown；done 只表示输入执行完成，unknown 表示可能生效、须观察后决定，skipped 不算执行失败 |
| delegation-result | title、child_agent_id；可含 role/delegation_id/child_task_id |
| fork-task-result | title、child_task_id |
| agent-task-result | send: operation/task_id/agent_name；status 另带 final_output/end_reason；当前 Task 状态由既有 Task 读取路径提供 |
| agent-configuration-draft | status=draft、agent_id、role、description、system_prompt；保存和编辑属于 Workbench，遵守调用方只读限制 |
| agent-team-configuration-draft | status=draft、team_id/name/description/scope/start_node/nodes/edges/configuration |
| agent-team-approval | team_run_id/team_id/name/goal/node_goals/start_node/nodes/edges/configuration；审批/执行状态读 TeamRun，不从展示字符串推断 |
| mcp-tool-result | action=call: server_name/tool_name/scope；action=list: servers[{server_name,scope,tools,error?}]，`scope` 为 system / workspace，单服务 error 不改变整个调用的生命周期 |
| skill-content | action=list: skills[{name,description,scope}]；action=load: skill_name/resource_path/scope，正文只进模型上下文 |

分页 page 为 offset/limit/has_more/next_offset。链接须经现有 URL 边界处理；本机图片经附件资源入口读取，不直接拼接本机路径。

## 5 修改与验证

修改 producer 时同步 kind 消费方、共享结果信号、测试和本文。绿地项目不保留旧展示结构兼容分支。`test_tool_registry_display_guard.py` 核对工具目录/本文清单，字段语义由各工具测试与前端对应视图测试验证。实时与冷重建继续复用原工具 metadata，不为展示折叠增加持久业务字段。
