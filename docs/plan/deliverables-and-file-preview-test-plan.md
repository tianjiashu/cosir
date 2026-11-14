# 交付物与文件预览：测试计划

> 配套方案：[`deliverables-and-file-preview-design.md`](deliverables-and-file-preview-design.md)（下称「方案」，§ 指其章节）；设计稿：[`assets/deliverables-ui-mockup.html`](assets/deliverables-ui-mockup.html)（下称「稿」）。
> 状态：计划稿（2026-11-15），只列要测什么、放在哪，不含测试代码。设计说明不在此重复。
> 现状核对：相关后端 pytest 8 个文件 75 项、前端 Vitest 12 个文件 135 项在当前工作区全部通过；Playwright E2E 未运行。

## 0. 约定

**层级代号**

| 代号 | 含义 | 入口 |
|---|---|---|
| PY | 后端 pytest | `apps/backend/tests/` |
| VT | 前端 Vitest（含 Electron main 的纯规则） | `apps/desktop/tests/unit/`，`npm run test:unit` |
| PW | Playwright E2E：Vite + 内存测试服务，无头 Chromium，宿主用 `fake-desktop-host.ts` 或不装（等于浏览器开发模式） | `apps/desktop/tests/e2e/` |
| PW-R | 同上，但文件接口改写到真实后端夹具 `apps/backend/tests/fixtures/workspace_files_server.py`（写法见 `workspace-files-integration.spec.ts` 的 `routeFilesToRealBackend`） | 同上 |
| DE | 桌面 E2E（`playwright.desktop.config.ts`，真实 Electron） | **本计划不排入**：`tests/desktop-e2e/desktop-app.ts` 直接 `electron.launch`，主窗口 `show: true`（`electron/main/window.ts:35`），会弹出可见窗口，违反「E2E 不弹窗」约束 |
| MAN | 人工实测（打包或开发版桌面应用），每项写明原因 | 结果记入方案 §8 的格式 |

**全局前提（影响多项测试的写法）**

| # | 前提 | 后果 |
|---|---|---|
| G1 | Playwright 自带的 Chromium 不含 H.264 / AAC 等专有解码器；Electron 含 | PW 的媒体播放只用 wav / ogg / webm（VP9/Opus）夹具；mov、mp4（H.264/HEVC）的可播放性只能 MAN |
| G2 | `mimetypes.guess_type` 读本机 `/etc/mime.types` 等文件（Windows 读注册表），本机实测 `.m4a`→`audio/mp4a-latm`、`.mmd`→`application/vnd.chipnuts.karaoke-mmd`、`.key/.pages/.numbers`→`None` | 凡断言 `audio`/`video` 归类或「认得 / 认不出」的 PY 测试，必须 monkeypatch `mimetypes` 或只用跨平台稳定的后缀（wav、mp3、mp4、mov、docx、zip、bin） |
| G3 | `fake-desktop-host.ts:42` 把 `platform` 写死为 `linux`，且没有 `thumbnail` / `quickLook` / `openExternally` | 阶段三的 PW 需要给假宿主加 `platform` 选项与三个可录制的假方法（返回固定 PNG），否则快速查看与缩略图分支测不到 |
| G4 | 前端 `snapshot-validation.ts` 严格校验 snapshot 顶层字段 | 阶段四加 `deliverables` 后，`test-server.mjs`、`tests/e2e/transport-fixtures.ts`、`tests/unit/transport-fixtures.ts` 必须同步补字段，否则全部 PW 失败 |

---

## 1. 新功能测试

编号规则：`S<阶段>-<序号>`；「兜底」列标出属于方案 §4.8 三层兜底中的哪一层（工具 / 类型 / 预览）。

### 1.1 阶段一：工具改名、三种条目、新卡片

| ID | 验证的行为与预期 | 层 | 文件 | 方案 | 兜底 |
|---|---|---|---|---|---|
| S1-01 | 注册表里只有 `present_deliverables`，没有 `present_files`；定义为 `quiet`、`needs_user_surface`、无 `user_decision`，主 Agent 允许 | PY | `tests/test_reply_presentation_tools.py`（改） | §3 | |
| S1-02 | strict schema：`items` 1–5 个，`target` 必填非空，`caption` 可 null，`extra=forbid`；0 个、6 个、旧字段 `files` 都被拒 | PY | 同上 | §3 | |
| S1-03 | 种类判定：`http://`、`https://`（大小写不敏感）→ `url`；已存在目录 → `directory`；已存在普通文件 → `file`；`ftp://`、`file://`、`mailto:`、`javascript:` 不当网址（按路径校验后失败并点名） | PY | 同上 | §3 | 工具 |
| S1-04 | 网址只校验协议、不联网（monkeypatch socket 断言零连接） | PY | 同上 | §3 | 工具 |
| S1-05 | 部分失败：有效条目进 `display_data.items`，无效条目只出现在 `content` 的点名清单；全部无效 → `status=error`、无 `kind`、`status_hint` 为受控短提示 | PY | 同上 | §3 | 工具 |
| S1-06 | 路径规则与 `read_file` 一致：设备路径（`/dev/null`）、禁读受保护路径（系统 `.cosir/.env`）被拒；工作区外绝对路径接受；目录走 `resolve_read_scope`（`/proc` 类伪文件树被拒） | PY | 同上；可复用 `tests/test_path_resolver_cosir_guard.py` 的夹具 | §3、§4.7 | 工具 |
| S1-07 | 不按格式拒绝：未知后缀二进制、无扩展名文本、`.ipynb`、零字节文件都能呈现 | PY | 同上 | §4.8 | 工具 |
| S1-08 | 同一次调用里同一指向写两遍只留一条（相对 / 绝对 / `./` 写法视为同一文件；网址按原文） | PY | 同上 | §4.6 | |
| S1-09 | 展示数据只有指向：`{type, path, absolute_path, caption}` / `{type:"url", url, caption}`，**不含**大小、mime、修改时间等现读事实；caption 去首尾空白、空串归 null | PY | 同上 | §4.1 | |
| S1-10 | 契约守卫：`deliverables` kind 登记在契约文档、前端目录覆盖新工具名（现有守卫自动覆盖，改名后须绿） | PY | `tests/test_tool_registry_display_guard.py`（不改，须通过） | §6 | |
| S1-11 | 前端读取：`readDeliverables` 接受 `items` 三种条目，丢弃形状不对的条目，一个不剩返回 null；旧形状 `files` 返回 null | VT | `tests/unit/reply-presentation-display.test.ts`（改） | §4.1 | |
| S1-12 | 工具目录：`present_deliverables` 有动词、图标，目标取 `items[].target` 首个加 `+N` | VT | `tests/unit/tool-catalog.test.ts`（补） | §3 | |
| S1-13 | 回合布局：`deliverables` 仍是独立产物；失败的调用在过程里出调用行，成功不出 | VT | `tests/unit/turn-layout.test.ts`（改 `present_files` 夹具） | §3 | |
| S1-14 | 条目组件：名称中间截断保住扩展名与结尾（含无扩展名、多点号 `a.tar.gz`、超长 CJK）；角标为大写后缀，目录 / 网址为图标；大小人类可读、目录写「文件夹」、网址写主机名 | VT | `tests/unit/deliverable-item.test.tsx`（新） | §4.3、§11.1 | |
| S1-15 | 大小与是否存在现读 `fileInfoQueryOptions`：与文件面板同一 query key（断言同一路径只发一次 `files/info`） | VT | 同上 | §4.1 | |
| S1-16 | 不存在（404 `FILE_NOT_FOUND`）→ 虚线、划线、不可点、写「已不存在」，不用警示色；无权限（403）→ 锁形角标、警示色「无访问权限」 | VT | 同上 | §4.3、§11.2 | |
| S1-17 | 点击路由：工作区文件进文件面板；目录在文件树展开（注意 `resolve-link.ts:96` 只靠末尾 `/` 认目录，条目组件须显式构造目录目标）；`.html` 与本机 / 局域网网址进当前 Session 内置浏览器；公网网址先确认再交系统浏览器；全部经 `use-open-link` | VT | `tests/unit/deliverable-item.test.tsx`；`tests/unit/open-link.test.tsx`（补目录用例） | §4.3 | |
| S1-18 | 音视频文件永远不进内置浏览器（即使后缀是 `.html` 之外、且 Session 有浏览器） | VT | `tests/unit/deliverable-item.test.tsx` | §4.3 | |
| S1-19 | 排序：方块一行 → 胶囊 → 音频；位图与 SVG 用自身画面（`ImageTile` 120×96），SVG 与动图不走静帧 | VT | 同上 | §11.1 | |
| S1-20 | 悬停 / 键盘焦点：提示条第一行说明、第二行完整路径（工作区内相对、外绝对）；大小位置换成「在文件夹中显示」（仅有宿主）；焦点有 `focus-ring` | PW | `tests/e2e/reply-presentation.spec.ts`（改） | §11.1–11.2 | |
| S1-21 | E2E 点卡片：工作区 Markdown 进文件面板并打开标签；目录在文件树展开并选中；本机网址在内置浏览器开标签（假宿主记录 `browser.open`）；公网网址出确认框 | PW | 同上 | §5 阶段一 | |
| S1-22 | 浏览器开发模式（不装假宿主）：没有「在文件夹中显示」，点击照常打开 | PW | 同上 | §11.2 | |
| S1-23 | 交付物与下一步建议同时出现：顺序为正文 → 交付物 → 建议；建议只在最后一条回复 | PW | 同上（现有第 2、3 条用例保留） | §11.5 | |
| S1-24 | 阶段一里工作区外的非网页文件：点击行为按方案定案（见 §4 问题 P2），测试断言定案后的那一种 | PW | 同上 | §4.7、§5 | |

### 1.2 阶段二：新类型、TIFF、表格与媒体预览

| ID | 验证的行为与预期 | 层 | 文件 | 方案 | 兜底 |
|---|---|---|---|---|---|
| S2-01 | `inspect_file` 判定表：`.csv`/`.tsv` 可解码 → `table`；不可解码 → `binary`；`.mmd`/`.mermaid` 可解码 → `mermaid`，不可解码 → `binary`；二进制 + `audio/*` → `audio`；二进制 + `video/*` → `video`；文本内容的 `.mp3`、TypeScript 的 `.ts`（mime 为 `video/mp2t`）仍是 `text` | PY | `tests/test_workspace_file_kinds.py`（新） | §4.2 | 类型 |
| S2-02 | 魔数优先于后缀：TIFF（`II*\0` / `MM\0*`）→ `image`、mime `image/tiff`；改成 `.png` 后缀的 TIFF 仍按头识别；`.tif` 后缀但内容是文本 → `text` | PY | 同上 | §4.2 | 类型 |
| S2-03 | 每个文件恰好一类：参数化遍历夹具目录，`kind` 必属十类之一，可解码为文本的落 `text`、其余落 `binary` | PY | 同上 | §4.8 | 类型 |
| S2-04 | mimetypes 依赖主机：monkeypatch `mimetypes.guess_type` 让 `.m4a` 返回 `None` → 归 `binary`（不是崩溃）；返回 `audio/x-foo` → `audio`（见 G2） | PY | 同上 | §4.2 | 类型 |
| S2-05 | 资源接口白名单：`audio`、`video` 返回 200 / 206，`Accept-Ranges`、`Content-Range` 正确，单段 Range、`bytes=-N`、416、`If-Range` 不匹配退整文件；`table`、`mermaid`、`text` 仍 400 | PY | `tests/test_workspace_files.py`（补 `test_resource_range_images_and_changed_version` 同款用例） | §4.2 | |
| S2-06 | 媒体不整文件读入内存：大文件（如 64 MB 稀疏文件）的 Range 请求只读所需字节（monkeypatch `source.read` 计数） | PY | 同上 | §4.2 | |
| S2-07 | TIFF 转码：小尺寸 TIFF 也返回 PNG；多页 TIFF 取第一页；损坏 TIFF → 400「图片内容无法识别」；超像素预算仍报错 | PY | 同上 | §4.2 | 预览 |
| S2-08 | TIFF 转码后的响应头不应让前端显示「大图缩略预览」（现有 `X-Cosir-Image-Preview: thumbnail` 只表示缩小，见问题 P20） | PY + VT | 同上；`tests/unit/workspace-files.test.ts` | §4.2 | |
| S2-09 | 前端 `FileInfo["kind"]` 联合类型含十类；`file-preview` 分发：`table`、`mermaid` 进可切换集合，`audio`、`video` 不可切源码 | VT | `tests/unit/file-preview-dispatch.test.tsx`（新） | §4.2、§4.4 | |
| S2-10 | 表格解析（papaparse）：自动识别 `,` / `\t` / `;`；引号内逗号与内嵌换行；BOM；空行；列数不齐的行；只有表头 | VT | `tests/unit/table-preview.test.tsx`（新） | §4.5 | |
| S2-11 | 表格交互：点表头排序升 → 降 → 还原；数字列判定（含千分位、负数、百分号、货币前缀的取舍按实现定）右对齐等宽；筛选按文本、「匹配数 / 总行数」；最左原始行号筛选排序后不变 | VT | 同上 | §11.3 | |
| S2-12 | 表格超限：大于 2 MB 时的表现按方案定案（见问题 P9）；不得把被截断的最后一行当成完整记录 | VT | 同上 | §4.5 | |
| S2-13 | 表格解析出错 → 上报「显示不了」，由分发处换成二进制空态并写原因 | VT | `tests/unit/file-preview-dispatch.test.tsx` | §4.8 | 预览 |
| S2-14 | mermaid 文件：内容作为一个 mermaid 代码块交给现有 Markdown 渲染组件；语法错误时的表现与 Markdown 内嵌 mermaid 一致（不白屏） | VT | `tests/unit/mermaid-file-preview.test.tsx`（新） | §4.2 | 预览 |
| S2-15 | 媒体预览：`<audio>` / `<video>` 的 `src` 直接是资源地址（带 `version`），不先取 blob；`error` 事件（code 4）→ 上报显示不了 → 二进制空态并说明原因 | VT | `tests/unit/media-preview.test.tsx`（新） | §4.2、§8 #5 | 预览 |
| S2-16 | E2E 表格：真实后端读 CSV / TSV，排序、筛选、行号、横向滚动只在表格内（页面 `scrollWidth == clientWidth`）、表头吸顶、切源码再切回 | PW-R | `tests/e2e/file-preview-kinds.spec.ts`（新） | §5 阶段二 | |
| S2-17 | E2E 音频播放：真实后端 wav / ogg，点播放后 `currentTime` 增长；拖动进度触发 206 请求 | PW-R | 同上 | §5 阶段二 | |
| S2-18 | E2E 视频：webm（VP9）可播放；伪装成 `.avi` 的非媒体文件触发降级到二进制空态 | PW-R | 同上 | §4.2、§4.8 | 预览 |
| S2-19 | E2E TIFF：文件面板显示转码后的图片 | PW-R | 同上 | §4.2 | |
| S2-20 | mov（原片、HEVC）、mp4（H.264）在桌面应用里能播放、能拖动 | MAN | — | §8 #5 | 原因：G1，PW Chromium 无专有解码器；DE 会弹窗 |

### 1.3 阶段三：宿主三个方法、系统缩略图、二进制空态

| ID | 验证的行为与预期 | 层 | 文件 | 方案 | 兜底 |
|---|---|---|---|---|---|
| S3-01 | `ipc.ts` 入参校验：三方法只接受字符串绝对路径（≤4096）、路径存在；`thumbnail` 边长为有限正整数且有上限；不合法抛 `HostUserError` 中文原因（与 `parseRevealPath` 同口径） | VT | `tests/unit/host-files-ipc.test.ts`（新；`ipc.ts` 目前没有任何单测，需 mock `electron` 或把解析函数提成纯模块） | §4.4 | |
| S3-02 | `quickLook`：先显示并聚焦主窗口再调用 `previewFile`；文件不存在不调用 `previewFile`、按缺失返回；非 macOS 抛明确错误 | VT | 同上（mock `BrowserWindow`） | §4.4、§8 #4 | |
| S3-03 | `openExternally` 不做后缀限制（`.command`、`.app` 也放行，按用户决定）；调用 `shell.openPath`，返回的错误字符串转成用户可见失败 | VT | 同上 | §4.4 | |
| S3-04 | 契约：`host-contract.ts` 的通道名与类型、preload 暴露、`lib/desktop-host` 失败消息透传（沿用 `desktop-host.test.ts` 写法） | VT | `tests/unit/desktop-host.test.ts`（补） | §4.4 | |
| S3-05 | 缩略图请求门槛：只对 PDF、视频与「认得出的文档 mime」请求；音频、未知二进制、无宿主、Linux 直接用 `WorkspaceFileIcon`；文件不存在时调用失败 → 图标 | VT | `tests/unit/deliverable-thumbnail.test.ts`（新） | §4.3 | 预览 |
| S3-06 | 缩略图缓存：按「路径 + 修改时间」缓存，同一文件多张卡片只调一次；修改时间变化重新取；先显示占位再换 | VT | 同上 | §4.3、§11.2 | |
| S3-07 | 二进制空态两种说法：mime 为通用二进制 → 「看起来是二进制数据」、突出「在文件夹中显示」、无快速查看；认得的格式 → 「没有内置预览」、主按钮「快速查看」；Windows 上主按钮换成「用系统应用打开」；「以文本方式查看」是文字链接 | VT | `tests/unit/binary-preview.test.tsx`（新） | §4.4、§11.3 | 预览 |
| S3-08 | 阅读工具栏「更多」含快速查看（仅 macOS）、用系统应用打开、在文件夹中显示，**所有类型**都有；无宿主时不出现 | VT | `tests/unit/reading-toolbar.test.tsx`（新） | §4.4 | |
| S3-09 | 降级统一在分发处：图片解码失败、PDF 损坏、表格解析失败、播放器失败都落到同一个二进制空态（带原因）；读取失败（网络、409 版本冲突、404）仍走 `FileReadProblem`，不伪装成二进制 | VT | `tests/unit/file-preview-dispatch.test.tsx` | §4.8 | 预览 |
| S3-10 | E2E（假宿主 darwin）：docx 空态显示假缩略图与三个按钮，点击后假宿主依次记录 `files.quickLook` / `files.openExternally` / `files.reveal`，参数是绝对路径 | PW | `tests/e2e/file-preview-kinds.spec.ts` | §5 阶段三 | |
| S3-11 | E2E（假宿主 win32）：无快速查看，「用系统应用打开」为主按钮 | PW | 同上 | §11.3 | |
| S3-12 | E2E 卡片：PDF / docx / 视频方块先显示占位、假缩略图到了再换；假宿主让 `thumbnail` 抛错 → 退回图标；不装宿主 → 退回胶囊 | PW | `tests/e2e/reply-presentation.spec.ts` | §4.3、§11.2 | 预览 |
| S3-13 | 系统缩略图真实画质、宽高比裁切铺满、首次 / 后续耗时 | MAN | — | §8 #3 | 原因：要操作系统生成，假宿主只能返回固定图 |
| S3-14 | 快速查看面板浮在主窗口之上、取得焦点、再次调用只换内容；主窗口隐藏在托盘时调用会先唤起主窗口 | MAN | — | §8 #4 | 原因：`previewFile` 弹出系统可见面板，无法无头验证 |
| S3-15 | 用系统应用打开：docx 用默认应用打开；`.command` 真的会执行（确认风险与方案一致） | MAN | — | §4.4 | 原因：启动外部程序 |

### 1.4 阶段四：snapshot `deliverables` 与侧栏「产出」卡

| ID | 验证的行为与预期 | 层 | 文件 | 方案 | 兜底 |
|---|---|---|---|---|---|
| S4-01 | 投影纯函数：工作副本里全部交付物 part 的条目；同一指向只留最后一次（相对 / 绝对写法同一文件算同一指向）；新的在前；失败的调用不计 | PY | `tests/test_deliverables_projection.py`（新） | §4.6 | |
| S4-02 | 撤回的回合不计（冷重建与热投影一致，沿用 `test_background_task_projection.py::test_withdrawn_rounds_are_not_listed` 写法） | PY | 同上 | §4.6 | |
| S4-03 | 继承来的消息（手动 fork）是否计入按方案定案（问题 P12），测试断言定案结果 | PY | 同上 | §4.6 | |
| S4-04 | 实时：交付物调用完成的那一帧带 `set deliverables` 变更；无关事件（token 帧）不重算 | PY | 同上（参照 `test_hot_projection_matches_cold_rebuild_through_cancellation`） | §4.6 | |
| S4-05 | 历史窗口：完整快照只带最近一段消息时，`deliverables` 仍含窗口之外的条目 | PY | `tests/test_snapshot_window.py`（补） | §4.6 | |
| S4-06 | 写入侧校验：`deliverables` 字段集合与取值词表精确校验，坏条目使物化失败而不缓存 | PY | `tests/test_state_snapshot_write_side_validation.py`（补） | §4.6 | |
| S4-07 | HTTP / JSON schema：`deliverables` 出现在 snapshot schema 与实际路由输出 | PY | `tests/test_snapshot_http_contract.py`（补） | §4.6 | |
| S4-08 | 前端 snapshot 校验接受 `deliverables`，拒绝缺字段与多余字段 | VT | `tests/unit/snapshot-validation.test.ts`（补） | §4.6 | |
| S4-09 | 卡片注册：`CONVERSATION_ASIDE_CARD_IDS` 新增 id，注册表每个 id 恰好一个实现；`useHasContent` 只在有条目时为真；折叠摘要为数量 | VT | `tests/unit/conversation-aside.test.ts`（补）、`tests/unit/deliverables-aside-card.test.tsx`（新） | §4.6、§11.4 | |
| S4-10 | 条目组件与回复卡共用：同一条目在两处的点击行为一致；不存在的划线变灰但保留 | VT | `tests/unit/deliverables-aside-card.test.tsx` | §11.4 | |
| S4-11 | E2E：有交付物的 Session 出现侧栏与页头入口，只有交付物（无后台工作）时也出现；点条目进文件面板；折叠显示数量；拖动排序与后台任务卡互换后刷新保持 | PW | `tests/e2e/deliverables-aside.spec.ts`（新） | §5 阶段四 | |
| S4-12 | E2E：撤回含交付物的回合后，侧栏条目消失；只剩被撤回的交付物时整张卡消失 | PW | 同上 | §4.6 | |

### 1.5 阶段五：工作区外交付物的授权读取

| ID | 验证的行为与预期 | 层 | 文件 | 方案 | 兜底 |
|---|---|---|---|---|---|
| S5-01 | 只认已记录的条目：按「会话、调用、第几个条目」取 info / text / resource 成功；不存在的会话、调用、下标，非交付物工具的调用，失败的条目 → 404；请求里不存在任何可传路径的参数 | PY | `tests/test_deliverable_files_api.py`（新） | §4.7 | |
| S5-02 | 跨会话：用 A 会话的调用 id 走 B 会话地址 → 404 | PY | 同上 | §4.7 | |
| S5-03 | 越界链接：记录后把文件换成指向别处的软链接 → 拒绝；**换成指向同目录子树里受保护文件（系统 `.cosir/.env`）的软链接也必须拒绝**（见问题 P4） | PY | 同上（参照 `test_opened_handle_rejects_link_swapped_after_resolution` 的 monkeypatch 换链写法） | §4.7 | |
| S5-04 | 读取复用：类型判定、分段文本、Range、版本冲突 409 与工作区接口同一套行为（参数化复用 S2-05 用例） | PY | 同上 | §4.7 | |
| S5-05 | 错误映射：文件被删 404 `FILE_NOT_FOUND`；`PermissionError` 403；与工作区接口同一组映射 | PY | 同上 | §8 #6 | |
| S5-06 | 撤回、删除 Session 后的读取结果按方案定案（问题 P13） | PY | 同上 | §4.7 | |
| S5-07 | 文件面板标签容纳「来自交付物」：与工作区标签同名时消歧；不能加入对话引用、不在文件树定位、不订阅文件监听；工具栏显示「工作区外 · 只读」 | VT | `tests/unit/workspace-files.test.ts`、`tests/unit/file-tab-label.test.ts`（补） | §4.7、§11.3 | |
| S5-08 | E2E：打开工作区外的 Markdown（渲染 / 源码可切）与图片；手动刷新拿到新内容；「加入对话」不存在 | PW-R | `tests/e2e/deliverables-outside-workspace.spec.ts`（新；真实后端夹具需能造一个工作区外的交付记录） | §5 阶段五 | |
| S5-09 | 删除被取代的分支：卡片上「工作区外不可点」的代码与对应测试断言移除（`reply-presentation.spec.ts` 里 `打开 chart.png` 计数为 0 的断言随之改写） | PW | `tests/e2e/reply-presentation.spec.ts` | §5 | |
| S5-10 | macOS 读桌面、文稿目录时的系统授权框时机；拒绝授权后卡片显示「无访问权限」、仍可在文件夹中显示 | MAN | — | §8 #6 | 原因：授权记在打包应用名下，开发期记在终端名下，只能在打包应用里看 |

### 1.6 阶段六：契约文档、AGENTS、`.ipynb`

| ID | 验证的行为与预期 | 层 | 文件 | 方案 | 兜底 |
|---|---|---|---|---|---|
| S6-01 | 契约文档第 3 节与 kind 表登记新工具名与新字段（现有守卫） | PY | `tests/test_tool_registry_display_guard.py` | §6 | |
| S6-02 | 提示词里提到的工具名都是已注册工具（新守卫；现在 `system_prompt_builder.py:517` 硬编码 `present_files`，没有任何测试会发现它过时） | PY | `tests/test_system_prompt_builder.py`（补） | §6 | |
| S6-03 | `.ipynb` 按 JSON 高亮并格式化显示；格式化只影响显示，不改文件；损坏 JSON 退回原文 | VT | `tests/unit/code-language.test.ts`（新，覆盖 `LanguageDescription.matchFilename` 的选择） | §8 #7 | 预览 |

---

## 2. 回归测试

每一行是一条被改动的现有链路。「保持不变」是必须守住的现有行为；「缺口」是现在没有测试、需要本次补上的。

| 链路 | 现有消费方 | 保持不变 | 已有测试 | 缺口（补在哪） |
|---|---|---|---|---|
| R1 工具改名 | `tool_names.py:39,74,110`、`schemas/__init__.py:25,64`、`define_agents.py:22,155`、`tool_registration.py:29,68`、`tool_models/__init__.py:17,49`、`tool-catalog.ts:150`、`system_prompt_builder.py:517`、契约文档 `:56,:82`、`AGENTS.md:275` | 只给侧栏会话；委派子 Agent、只读会话拿不到；静默呈现；`offer_next_step` 完全不受影响 | `test_session_tool_scope.py`（`_USER_SURFACE_ONLY` 改名）、`test_tool_registry_display_guard.py`、`test_reply_presentation_tools.py`、`tool-catalog.test.ts` | 提示词工具名守卫（S6-02）；旧名全仓零残留（`git grep present_files` 只剩文档历史，作为交付检查项，不写测试） |
| R2 展示数据形状 `files`→`items` | `reply-presentation-display.ts`、`artifact-zone.tsx:38`、`turn-layout.ts:14`、`message-height-estimate.ts:111-114,158`（按 `display_data.files` 计数） | 回合布局把交付物当独立产物；等用户决定期间不显示；渲染错误被 `RenderIsolationBoundary` 隔离 | `turn-layout.test.ts:127-134`、`reply-presentation-display.test.ts`、`render-isolation.test.tsx` | **估高**：改名后 `deliverableCount` 静默返回 0，现有 `message-height.test.ts` 没有交付物用例，补「n 个条目的估高 > 0 且随换行增加」 |
| R3 `FileInfo.kind` 六→十类 | 后端 `file_content.py:66-84`、`file_models.py:38`、资源白名单 `workspace_files_api.py:128`；前端 `lib/api/workspace-files.ts:25`、`file-preview.tsx:17,69-73`、链接图标与文件图标 | 原六类判定结果逐个不变（PDF、PNG、JPEG、GIF、BMP、WEBP、AVIF、ICO、SVG、Markdown、文本、二进制）；`.md` 仍是 `markdown` | `test_workspace_files.py`（只断言了 `text` 与链接目标，判定表无系统覆盖） | 判定表全量参数化（S2-01 同文件，先写现有六类作为基线再加新类） |
| R4 资源接口白名单 | `workspace_files_api.py:122-135`、`WorkspaceResourceResponse.py` | 图片、SVG、PDF 的 Range、416、If-Range、409、大图缩略、SVG 8 MB 上限 413、文本 400 | `test_http_contract_and_resource_range`、`test_resource_range_images_and_changed_version` | 无新增；两条现有用例必须原样通过 |
| R5 文件面板分发与二进制空态 | `file-preview.tsx:59-90`、`image-preview.tsx:52-53`、`pdf-preview.tsx:155,233`、`file-read-problem.tsx` | 带行号打开先切源码；每文件「渲染 / 源码」偏好；`文件已更新` 提示；文件不存在 / 工作区不存在的中性空态与「查找同名文件」；`以文本方式查看` 仍可达 | PW `workspace-files.spec.ts`（Markdown、图片、PDF 阅读器、偏好、窄宽度工具栏、文件不存在）、`file-read-problem.test.ts` | 分发处单测目前没有（S2-09、S3-09 新建）；**有意改变**：图片解码失败与 PDF 读取失败从各自的 `ErrorState` 改为二进制空态，要在 PR 说明里列为行为变化 |
| R6 链接打开 | `use-open-link.ts`、`resolve-link.ts`；对话正文、Markdown、终端、工具卡片共用 | 工作区路径带行号定位；本机 HTML 进内置浏览器、无 Session 退回文件面板；公网链接先确认；迟到的打开被丢弃 | `open-link.test.tsx`（20 项）、`resolve-link.test.ts`、PW `workspace-files.spec.ts:630,660`、`link-icons.spec.ts` | 条目组件不得为交付物另写解析规则（S1-17 断言经 `useOpenLink`）；目录目标补一条用例 |
| R7 「在文件夹中显示」 | `ipc.ts:68-77`、`deliverables-card.tsx`、下载与后台任务 | 不存在的路径给「文件已不存在」 | PW `reply-presentation.spec.ts` 第 1 条（断言 `files.reveal` 参数） | `parseRevealPath` 无单测，随 S3-01 一并补 |
| R8 会话侧栏卡片注册表 | `aside-card-registry.tsx`、`conversation-aside-store.ts`、页头入口、`revealBackground` | 后台任务卡的折叠、拖动、定位到条目；没有任何内容时侧栏与入口都不出现；宽窄两种摆放 | `conversation-aside.test.ts`、PW `conversation-aside.spec.ts`（11 条）、`background-surfaces.spec.ts`、`background-tasks.spec.ts` | **有意改变**：`normalizeConversationAsidePreferences`（`conversation-aside-store.ts:43-45`）要求 `order` 长度等于卡片数，新增卡片后旧偏好整体重置为默认（含「关闭侧栏」），测试要改成断言这一行为；`conversation-aside.spec.ts:267` 的「没有后台工作时不出现」要补「也没有交付物」前提 |
| R9 snapshot 顶层字段 | 后端 `conversation_state_snapshot.py`、`conversation_state_service.py:151,248-254,423`；前端 `contract.ts`、`snapshot-validation.ts`、`test-server.mjs:408`、两份 `transport-fixtures.ts` | 其余顶层字段（`background_tasks`、`foreground_waits`、`handoffs`、`terminal_sessions`）的刷新时机与帧内容不变；读取不校验只裁窗口 | `test_background_task_projection.py`、`test_state_snapshot_write_side_validation.py`、`test_snapshot_window.py`、`snapshot-validation.test.ts` | 见 G4；新字段的刷新点不能挂在后台任务的 `event.background is not None` 分支上（S4-04） |
| R10 文件面板标签寻址（阶段五） | `workspace-files-store.ts`（按工作区相对路径记标签、偏好、位置）、`file-tab-label.ts`、`add-to-chat-button.tsx`、`use-file-watch.ts`、`file-tree.tsx` 定位 | 工作区标签的固定、同名消歧、溢出菜单、关闭后目录入口、监听实时更新 | PW `workspace-files.spec.ts:546`、`workspace-files-integration.spec.ts:70,115`、`file-tab-label.test.ts`、`workspace-files.test.ts` | 「来自交付物」标签与工作区标签共存的用例（S5-07） |
| R11 图片外观 | `components/image/image-tile.tsx`、`image-strip.tsx`、`lib/image/tile-size.ts` | 用户消息图片、工具图片的尺寸、失败占位、灯箱翻页 | `message-images.spec.ts`、`image-read-view.test.ts` | 无 |
| R12 产物区顺序 | `artifact-zone.tsx`（改动卡在前，其余按调用顺序）、`turn.tsx:119-120` | 改动卡仍在交付物之前（方案 §11.5 未提改动卡，见问题 P21） | `turn-layout.test.ts:100-114` | 一个回合里有多次交付物调用时的排列（每次调用一组，还是合并） |

---

## 3. UI 视觉回归

### 3.1 现状

**仓库没有视觉回归能力。** 全仓无 `toHaveScreenshot` / `toMatchSnapshot`，Playwright 配置里无 `snapshotDir`、`snapshotPathTemplate`、`expect.toHaveScreenshot` 阈值，也没有 `*-snapshots` 基线目录。现有约 40 处 `page.screenshot({ path })`（如 `conversation-aside.spec.ts:47`、`link-icons.spec.ts:66`、`workspace-files.spec.ts:182`）只把图片写进 `test-results/`（已 gitignore）或 `/private/tmp`，供人眼查看，不比对、不失败。

### 3.2 引入方案

| 项 | 做法 |
|---|---|
| 机制 | Playwright 内置 `expect(locator).toHaveScreenshot()`，不引入新依赖 |
| 隔离 | `playwright.config.ts` 新增 project `visual`，`testMatch: /\.visual\.spec\.ts$/`；默认 project 用 `testIgnore` 排除它，日常 `npm run test:e2e` 不跑；新增脚本 `test:e2e:visual` |
| 基线位置 | `snapshotPathTemplate: "tests/e2e/__screenshots__/{platform}/{testFilePath}/{arg}{ext}"`，入库 |
| 稳定性 | 固定 viewport（1280×800，另一档 720 宽测窄宽度）、`deviceScaleFactor: 1`、`animations: "disabled"`、`caret: "hide"`、`reducedMotion: "reduce"`；深浅色用 `emulateMedia({ colorScheme })`（`link-icons.spec.ts:52` 已有写法）；动态内容（时间、时长）`mask`；`maxDiffPixelRatio: 0.01` |
| 确定的素材 | 缩略图由假宿主返回固定 PNG；动图夹具用静态 GIF（`animations: "disabled"` 停不住 GIF）；视频用固定首帧的 webm 并在截图前暂停于 0 秒 |
| 平台 | 字体栈是系统字体（`globals.css:102`，PingFang / 微软雅黑 / Noto），不同系统渲染不同：基线只在 macOS 生成和比对，其他平台跳过；设置了 `COSIR_PLAYWRIGHT_EXECUTABLE` 时 visual project 跳过（浏览器不同像素不同） |
| 更新 | 有意改样式时 `--update-snapshots` 重录，基线差异随 PR 人工审 |

**代价**：约 42 张基线，每张 20–150 KB，入库共约 3–4 MB；每次样式改动要重录并审图；只在 macOS 有效，CI 若在 Linux 需另录一套或不跑；首次接入约 0.5–1 天（配置、夹具、假宿主扩展）。不引入时的替代是保留现有的 `outputPath` 截图加人工看图，零维护但不防回归。

### 3.3 需要基线的界面与状态

规格以方案 §11 与稿为准。每行默认录浅色与深色两张（「深浅」列为「仅浅」的除外）。

| 界面 | 状态 | 依据 | 深浅 | 文件 |
|---|---|---|---|---|
| 胶囊组 | 默认：文件 ×2、目录、网址（稿 A） | §11.1 | 两张 | `tests/e2e/deliverables.visual.spec.ts`（新） |
| 胶囊 | 悬停：底色变灰、大小换成「在文件夹中显示」、提示条两行 | §11.1 | 两张 | 同上 |
| 胶囊 | 键盘焦点 `focus-ring` | §11.2 | 仅浅 | 同上 |
| 胶囊 | 超长中文文件名中间截断，保住 `_v2.csv` | §11.1 | 仅浅 | 同上 |
| 胶囊组 | 窄宽度（720）换行 | §11.1 | 两张 | 同上 |
| 方块行 | 两张图片 + PDF + 视频（带播放标记）（稿 B） | §11.1 | 两张 | 同上 |
| 方块 | 缩略图加载中的占位（角标加文件名） | §11.2 | 仅浅 | 同上 |
| 音频播放条 | 未播放 | §11.1 | 两张 | 同上 |
| 异常 | 不存在（虚线、划线）、无访问权限（锁、警示色）、工作区外（与普通一致）（稿 C） | §11.2 | 两张 | 同上 |
| 无宿主 | PDF / Office / 视频退回胶囊 | §11.2 | 仅浅 | 同上 |
| 组合 | 正文 → 交付物 → 下一步建议（稿 D） | §11.5 | 两张 | 同上 |
| 表格视图 | 默认、按数字列降序、筛选后（「38 / 1,240 行」）、宽表横向滚动到中段 | §11.3 | 两张（默认），其余仅浅 | `tests/e2e/file-preview.visual.spec.ts`（新） |
| 表格视图 | 超限提示行 | §11.3 | 仅浅 | 同上 |
| mermaid 文件 | 渲染视图 | §4.2 | 两张 | 同上 |
| 二进制空态 | 认得的文档（darwin，快速查看为主按钮） | §11.3 | 两张 | 同上 |
| 二进制空态 | 认得的文档（win32，系统应用打开为主按钮） | §11.3 | 仅浅 | 同上 |
| 二进制空态 | 认不出的二进制（通用图标、无快速查看） | §11.3 | 两张 | 同上 |
| 二进制空态 | 预览降级（播放器失败，带原因） | §4.8 | 仅浅 | 同上 |
| 阅读工具栏 | 「更多」菜单展开 | §4.4 | 仅浅 | 同上 |
| 视频面板 | 黑底等比居中、原生控件 | §11.3 | 两张 | 同上 |
| 音频面板 | 播放器 | §4.2 | 仅浅 | 同上 |
| 工作区外标签 | 工具栏「工作区外 · 只读」、路径折叠中段（阶段五） | §11.3、§10.4 | 两张 | 同上 |
| 侧栏「产出」卡 | 展开（含不存在的划线条目）（稿 4） | §11.4 | 两张 | `tests/e2e/deliverables-aside.visual.spec.ts`（新） |
| 侧栏「产出」卡 | 折叠显示数量 | §11.4 | 仅浅 | 同上 |
| 侧栏 | 窄视口浮层里与后台任务卡并列 | §11.4 | 仅浅 | 同上 |

设计稿本身不做基线：它是占位画法（文档、视频缩略图是手绘），只用于人工对照。

---

## 4. 动笔写测试前需要定案的问题

下列问题让对应测试无法写出唯一的预期，需在实施前由方案定案（写回方案对应小节）。

> 2026-11-15：21 条已全部在方案 §12 定案并写回正文，对应测试的预期以方案正文为准。P15 附带一项测试改动：假宿主的平台改为可配置（默认 `darwin`），缩略图与快速查看的用例按平台分别断言。

| # | 问题 | 证据 | 影响的测试 |
|---|---|---|---|
| P1 | 图片点开去哪：§1 偏离表写「文件面板（已有缩放）」，§11.1 与稿写「点开进灯箱」 | 方案 `:27` vs `:378`；稿 `:191` | S1-17、S1-21 |
| P2 | 阶段一至四里工作区外文件的卡片：§4.1 说大小、类型、是否存在一律现读文件服务，但文件服务只收工作区相对路径并按句柄拒绝越界；§4.7 说此前「有系统缩略图、快速查看和另两个出口」，可快速查看与系统应用打开只在文件面板的二进制空态里，工作区外文件进不了面板，卡片上只有「在文件夹中显示」；缩略图门槛还要 `FileInfo.mime`。§11.2「外观与普通胶囊一致」又与阶段五才删除的「工作区外不可点」矛盾 | 方案 `:96`、`:120`、`:123`、`:165`、`:187`、`:391`；`file_access.py:56-57`、`workspace_files_api.py:111-115` | S1-16、S1-24、S3-12 |
| P3 | 工作区外的目录：「目录在文件树里展开」只对工作区内成立；阶段五只授权文件的 info / text / resource | 方案 `:117`、`:161-162` | S1-17、S5-01 |
| P4 | 授权读取的校验范围取「文件所在目录」，而句柄校验是 `is_relative_to`，接受整棵子树：交付 `~/report.md` 后把它换成指向 `~/.cosir/.env` 的软链接仍落在 `~` 之内；受保护路径只在工具调用时查过一次 | 方案 `:160`、`:162`；`file_access.py:56` | S5-03 |
| P5 | 403 有两种原因：系统授权被拒与软链接越出工作区共用同一个 403 和同一句文案；卡片按状态码显示「无访问权限」会把越界软链接也画成锁 | `workspace_files_api.py:51-52`、`file_access.py:57`；方案 `:244`、`:390` | S1-16、S5-05 |
| P6 | 「认得 / 认不出」与 `audio` / `video` 归类都依赖 `mimetypes`，结果随机器变：`.key/.pages/.numbers` 得不到 mime，会被判成「认不出」、没有快速查看，而快速查看恰好能看它们 | 方案 `:108-109`、`:129-134`；本机实测见 G2 | S2-01、S2-04、S3-07 |
| P7 | 「`FileInfo.mime` 认得出的文档类型」没有给出清单，缩略图门槛无法断言 | 方案 `:120` | S3-05 |
| P8 | 视频可播放性的结论（§8 #5）只在 macOS 硬件解码下成立；Windows 上 HEVC 需要系统扩展，PW Chromium 连 H.264 都没有 | 方案 `:243` | S2-18、S2-20 |
| P9 | 表格超过 2 MB：§4.5「提示切到源码，不做分页解析」、§11.3「底部一行说明并给以源码查看全部」、稿「只显示前 N 行」三种说法；且超限时分段读文本只给 512 KB，最后一行可能被截断 | 方案 `:148`、`:397`；稿 `:240`；`file_content.py:134` | S2-12 |
| P10 | 二进制空态文案与按钮三处不一：§4.4「此类型没有内置预览 / 看起来是二进制数据」、四个按钮；§11.3 认不出的没有快速查看、文字链接；稿「这种文件没有内置预览 / 这个文件不是文本，无法预览 / 仍以文本方式查看」；现有代码是「此类型无内置内容预览」 | 方案 `:127`、`:133-134`、`:398`；稿 `:257`、`:269`、`:271`；`file-preview.tsx:85` | S3-07、S3-10 |
| P11 | §4.8 统一降级没区分「预览器显示不了」与「读取失败」：SVG 超 8 MB 的 413 原意是「切源码分段阅读」，图片像素超预算是 400，PDF 读取失败可能只是网络 | 方案 `:175-177`；`workspace_files_api.py:132-133`、`image_preview.py:25`、`pdf-preview.tsx:155` | S3-09 |
| P12 | `deliverables` 投影：刷新时机没写（后台看板只在带后台记录的状态事件上重算，照抄会永不刷新）；继承消息是否计入没写（看板跳过继承，现有交付物卡却展示继承的）；「同一指向」的等价规则没写（相对 / 绝对、网址末尾斜杠与片段）；侧栏条目要给阶段五寻址，需带调用 id 与下标，形状没定 | 方案 `:152`；`conversation_state_service.py:248-254`、`conversation_state_background_task.py:79-80`、`deliverables-card.tsx:27` | S4-01、S4-03、S4-04 |
| P13 | 撤回后行不删除、只移出投影；授权读取按记录寻址，没说被撤回的交付物还能不能读 | 方案 `:160-161`；`AGENTS.md` 撤回一节 | S5-06 |
| P14 | 胶囊「整枚是按钮」，悬停时大小位置换成「在文件夹中显示」（稿里是按钮内的 `span`）：按钮里嵌第二个操作既不合法也无法用键盘触发；§11.2 又说焦点与悬停同态；「无访问权限」时「点击仍可在文件夹中显示」让同一次点击按状态换语义 | 方案 `:373`、`:377`、`:388`、`:390`；稿 `:164-168` | S1-20、视觉基线「悬停」「焦点」 |
| P15 | Linux 与假宿主：缩略图只写了 macOS、Windows，Linux 行为没写；假宿主平台写死 `linux` | 方案 `:140`；`fake-desktop-host.ts:42` | S3-05、S3-12 |
| P16 | 胶囊横排换行后高度取决于宽度，估高按「条目数 × 行高」不再成立；改名后现有估高静默变 0 | `message-height-estimate.ts:111-114`、`:158`；方案 `:208` | R2 |
| P17 | 指向音视频的网址条目（如 `http://localhost:8000/demo.mp4`）按本机地址进内置浏览器，会落进下载；「音视频永远不交给内置浏览器」只约束了文件 | 方案 `:119`；`use-open-link.ts:53-54` | S1-18 |
| P18 | 「浏览器内核不支持的图片一律转 PNG」言过其实：类型只按魔数认图片，HEIC（`ftypheic`）等仍是 `binary`，不会转码 | 方案 `:111`；`file_content.py:68-84` | S2-02、S2-07 |
| P19 | 卡片缩略图复用文件面板的图片资源，而它整张下载成 blob（`no-store`，上限 32 MB），历史里每张图片卡都会重新下载原图 | 方案 `:120`；`resources.ts:16-40`、`image_preview.py:12` | 性能，无对应断言；建议定案时考虑缩略尺寸 |
| P20 | TIFF 小图转码会带上 `X-Cosir-Image-Preview: thumbnail`，前端据此显示「大图缩略预览」 | `WorkspaceResourceResponse.py:47`、`image-preview.tsx:48` | S2-08 |
| P21 | §11.5 只规定正文 → 交付物 → 建议，没提改动卡（现在产物区里改动卡排在最前）与一个回合多次交付物调用如何排列；§11.4 说折叠时显示数量，稿在展开态也显示 | 方案 `:408`、`:404`；`artifact-zone.tsx:43`；稿 `:293` | R12、S4-09 |

## 5. 数量汇总

| 部分 | 项数 |
|---|---|
| 新功能测试（阶段一 24、二 20、三 15、四 12、五 10、六 3） | 84，其中 MAN 5 |
| 回归链路 | 12 |
| 视觉基线条目（深浅色合计约 42 张图） | 24 |
| 需定案问题 | 21 |
