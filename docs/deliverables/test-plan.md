# 交付物与文件预览：测试计划

> 配套方案：[`design.md`](design.md)（下称「方案」，§ 指其章节）；设计稿：[`ui-mockup.html`](ui-mockup.html)（下称「稿」）；对标截图：[`benchmark/`](benchmark/)。
> 状态：计划稿（2026-11-15，按方案第三轮修订同步）。只列要测什么、放在哪，不含测试代码；方案内容不在此复述。
> 上一版查出的 23 个问题已在方案 §12 第三轮定案，对照见 §6；本文的预期都以定案为准。

## 0. 约定

**层级代号**

| 代号 | 含义 | 入口 |
|---|---|---|
| PY | 后端 pytest | `apps/backend/tests/` |
| VT | 前端 Vitest（含 Electron main 的纯规则） | `apps/desktop/tests/unit/`，`npm run test:unit` |
| PW | Playwright E2E：Vite + 内存测试服务，无头 Chromium；宿主用 `tests/e2e/fake-desktop-host.ts`，不装即浏览器开发模式 | `apps/desktop/tests/e2e/` |
| PW-R | 同上，文件接口改写到真实后端夹具 `apps/backend/tests/fixtures/workspace_files_server.py`（写法见 `workspace-files-integration.spec.ts` 的 `routeFilesToRealBackend`） | 同上 |
| PW-V | 视觉基线（§3，新增 project，默认不跑） | `*.visual.spec.ts` |
| WIN-A | 在 Windows 上跑的 PY / VT / PW（本机或新增的 Windows CI job） | §4 |
| WIN-M | Windows 实机人工 | §4 |
| MAN | macOS 人工（开发版或打包应用），每项写明为什么不能自动化 | §5 |

**不排入桌面 E2E（`playwright.desktop.config.ts`）**：`tests/desktop-e2e/desktop-app.ts` 直接 `electron.launch`，主窗口 `show: true`（`electron/main/window.ts:35`），会弹出可见窗口，违反「E2E 只能无头或隐藏窗口」。需要真实宿主的项一律列为 MAN。

**全局前提**

| # | 前提 | 对测试写法的约束 |
|---|---|---|
| G1 | Playwright 自带 Chromium 不含 H.264 / AAC / HEVC 解码器（方案 §8 #5） | PW 媒体只用 wav、ogg、webm（VP9/Opus）夹具；mov、mp4 只能 MAN / WIN-M |
| G2 | `mimetypes` 随主机变（本机实测 `.m4a`→`audio/mp4a-latm`、`.mmd`→`application/vnd.chipnuts.karaoke-mmd`、`.key`→`None`、`.ts`→`video/mp2t`） | 归类与音视频 MIME 的测试一律 monkeypatch `mimetypes.guess_type` 为返回 `None` 或乱值，断言结果不变（方案 §4.2 的显式后缀表） |
| G3 | `fake-desktop-host.ts:42` 写死 `platform: "linux"`，没有 `thumbnail` / `quickLook` / `openExternally`，`browser.open` 直接抛错（`:68`） | 阶段二起给假宿主加可录制的 `browser.open`；阶段四加 `platform` 选项（默认仍 linux）与三个可录制的假方法（返回固定 PNG） |
| G4 | 前端 `snapshot-validation.ts` 严格校验顶层字段；13 个文件内联了完整 snapshot（`grep -l foreground_waits tests`：`test-server.mjs`、两份 `transport-fixtures.ts`、`background-tasks.test.tsx` 及 9 个 spec） | 阶段二加 `deliverables` 时一次改完全部夹具（S2-46），否则全部 PW 失败 |
| G5 | 测试服务 `test-server.mjs` 没有文件接口 | PW 的文件信息、资源、监听流用 `page.route`；需要真实行为（Range、监听、转码）用 PW-R |
| G6 | 大小写 / firmlink / NFD 用例依赖卷特性 | 大小写用例先探测卷是否大小写不敏感，不满足就 skip（写法同 `test_workspace_service_create_reuse.py:108`）；firmlink 用例只在 darwin 跑；大小写敏感卷用例在 Linux CI 跑 |

编号：新功能 `S<阶段>-<序号>`；回归 `R<序号>`；视觉 `V<序号>`；Windows `W<序号>`；人工 `M<序号>`。「兜底」列标方案 §4.10 的层（工具 / 类型 / 预览）。

---

## 1. 新功能测试

### 1.1 阶段一：路径身份（§4.7）

新增测试文件 `apps/backend/tests/test_file_identity.py`（原语）与 `test_path_identity_call_sites.py`（各调用点的端到端口径）。

| ID | 验证什么与预期 | 层 | 文件 | 方案 |
|---|---|---|---|---|
| S1-01 | `FileIdentity`：同一文件的大小写写法、firmlink 写法（`/System/Volumes/Data/...`）、NFD/NFC 写法、符号链接、硬链接身份相等；不同文件不等 | PY | `test_file_identity.py` | §4.7 |
| S1-02 | `RootDirectory.contains`：根本身、子孙、尾部一级与多级不存在（写新文件）、大小写 / firmlink / NFD 写法均在内；同前缀兄弟（`.cosir2`、`.cosir_v2`）不在内 | PY | 同上 | §4.7 |
| S1-03 | 先解析再比身份：根内符号链接指向根外、`ws/../out/x`、`ws/nope/../../out/new.txt` 均不在内（不先 `realpath` 时这三种都会误判在内，已模拟验证） | PY | 同上 | §4.7 |
| S1-04 | 根外的链接指向根内（如指向系统 `.cosir`）判在内；断链与悬空链接不抛异常、按目标不存在处理 | PY | 同上 | §4.7 |
| S1-05 | `RootDirectory.relative` 返回 POSIX 相对路径，取自解析结果而非输入写法；根外按契约抛出（断言实现选定的那一种异常） | PY | 同上 | §4.7 |
| S1-06 | 根 `st_ino == 0`（monkeypatch `os.stat`）时 `RootDirectory.at` 显式报错，不退化成「整卷在根内」 | PY | 同上 | §4.7 |
| S1-07 | `canonical_path` / `canonical_path_of`：大小写写法还原成磁盘大小写、firmlink 前缀去掉、链接还原成目标、目录可取（darwin 走 F_GETPATH，本机已实测四条都成立） | PY | 同上 | §4.7 |
| S1-08 | 硬链接：两个名字身份相等，规范路径可以不同（macOS 报最近打开的名字），因而 `key` 可以不同——按残留接受，断言不抛异常、两个结果都是存在的路径 | PY | 同上 | §4.7 残留 |
| S1-09 | 受保护路径绕过修复：`~/.COSIR/.env`、firmlink 写法、NFD 写法下的 `.env` 与 `config/models.json`，`read_file`、`search_content` 作用域、`write_file` 全部拒绝；`skills/` 的大小写写法仍只读放行。现状红（已实测 `is_within` 对大小写、firmlink 都返回 False） | PY | `test_protected_paths.py`（补） | §4.7 |
| S1-10 | 硬链接把 `.cosir/.env` 链进工作区：现状可读，标 `xfail(strict=True)`，原因指向 AGENTS 待办「安全与权限」 | PY | 同上 | §4.7 残留 |
| S1-11 | 遍历剪枝：从系统 `.cosir` 的上级起搜，`.COSIR` 写法的根与 NFD 写法目录名都被剪掉；名字同为 `.cosir` 的工作区 `.cosir` 不被剪（名字命中、身份不同） | PY | 同上 | §4.7 |
| S1-12 | 写入解析：工作区内文件以大小写或 firmlink 绝对路径写入被接受（现状报 `path escapes project root`，红）；可写根之外、工作区 `.cosir`（含 `.COSIR` 写法）仍拒绝 | PY | `test_path_identity_call_sites.py` | §4.7 |
| S1-13 | 可写根 `narrow`：大小写写法的子目录可收窄；指向上级之外的链接仍拒；保护区（大小写写法）仍拒 | PY | 同上 | §4.7 |
| S1-14 | 原子写：containment 根以不同写法传入时可写；祖先链中途换成指向外部的链接仍 `EPERM`；多级新建目录在根内 | PY | 同上 | §4.7 |
| S1-15 | `execute_terminal` 工作目录：大小写写法的绝对 `workdir` 在根内被接受；`..` 与外指链接仍报 `escapes workspace root` | PY | 同上 | §4.7 |
| S1-16 | 技能目录：作用域内的 Skill 目录链接到作用域外被略过并记 `skill_directory_outside_scope`（`_scan_scope` 现无测试）；资源越界仍拒 | PY | `test_mcp_skill_tools.py`（补） | §4.7 |
| S1-17 | 附件 `_inside`：大小写写法在附件目录内通过；`..` 与链接逃逸仍报 `ATTACHMENT_NOT_OWNED` | PY | `test_path_identity_call_sites.py` | §4.7 |
| S1-18 | 工作区登记：以大小写、firmlink、尾斜杠写法登记同一目录得到同一记录，`root_path` 存规范路径；大小写敏感卷上只差大小写的两个目录是两条记录 | PY；后半 Linux CI | `test_workspace_service_create_reuse.py`（改 `:104`、补） | §4.7 |
| S1-19 | 回归证明：工作区以非磁盘大小写或 firmlink 写法登记后，`info` / `text` / `resource` / `directory` 都能读（现状每个文件 403，已实测）；换链攻击仍拒（现有 `test_workspace_files.py:199`、`:365` 保持） | PY | `test_workspace_files.py`（补） | §4.7 回归证明 |
| S1-20 | 目录树环检测改用身份后：目录链接回祖先仍标「目录链接形成循环」，两条不同路径指向同一内部目录仍可展开 | PY | `test_workspace_files.py:50`（保持） | §4.7 |
| S1-21 | 删除核对：`app.utils.path.validation` 不可导入、`is_trusted_cosir_path` 不存在；`app/utils/path/__init__.py:30,65` 的再导出一并删除 | PY | `test_file_identity.py` | §5 删除列 |

### 1.2 阶段二：交付记录、卡片、清单投影与监听（§3、§4.1、§4.3、§4.6、§4.8）

| ID | 验证什么与预期 | 层 | 文件 | 方案 | 兜底 |
|---|---|---|---|---|---|
| S2-01 | 注册表只有 `present_deliverables`：`quiet`、`needs_user_surface`、无 `user_decision`；委派子 Agent 与只读会话拿不到 | PY | `test_reply_presentation_tools.py`（改）、`test_session_tool_scope.py:25`（改名） | §3 | |
| S2-02 | strict schema：`items` 1–5，`target` 必填，`caption` 可 null，多余字段拒；`caption` 的参数描述不再含「显示在文件名下方」 | PY | `test_reply_presentation_tools.py` | §3 | |
| S2-03 | 种类判定：`http://`、`HTTPS://` → url；已存在目录 → directory；文件 → file；`ftp://`、`file://`、`mailto:`、`javascript:` 按路径校验后失败并点名 | PY | 同上 | §3 | 工具 |
| S2-04 | 网址只校验协议、不联网（monkeypatch socket 断言零连接） | PY | 同上 | §3 | 工具 |
| S2-05 | 部分失败：有效的进 `delivery`，无效的只在 `content` 点名；全部无效 → `error`、受控 `status_hint`；成功时 `display_data` 为空 | PY | 同上 | §3、§4.1 | 工具 |
| S2-06 | 路径规则与 `read_file` 一致：设备路径、禁读受保护路径（含 S1-09 的写法变体）拒；工作区外绝对路径收；目录走读取作用域规则 | PY | 同上 | §3 | 工具 |
| S2-07 | 不按格式拒绝：未知后缀二进制、无扩展名文本、`.ipynb`、零字节文件都能交付 | PY | 同上 | §4.10 | 工具 |
| S2-08 | 记录字段：file 带 `key`（规范路径）、`workspace_path`（内为相对 POSIX、外为 null）、`absolute_path`、`kind`、`size`、`system_preview`；directory 无 kind；caption 去空白、空串归 null | PY | 同上 | §4.1 | |
| S2-09 | 工作区外文件的观察：以文件所在目录为根调用 `inspect_file`，记录拿到 `kind`、`size`、`system_preview`（PDF、docx、zip、文本各一）；外部路径是指向别处的链接时同样成功 | PY | 同上 | §4.1 | |
| S2-10 | 网址 `key`：协议与主机小写、去默认端口、空路径补 `/`、去片段——`http://LOCALHOST:5173`、`http://localhost:5173/`、`http://localhost:5173/#x` 同一 `key`；`http://a:80/` 与 `http://a/` 同一；`media`：路径后缀查音视频表，查询串与片段不影响，无后缀为 null | PY | 同上 | §4.1 | |
| S2-11 | 调用内去重：`a.md`、`./a.md`、绝对路径、大小写写法只留一条，取最后一次 | PY | 同上 | §4.1 | |
| S2-12 | 流向：`ToolObservation.delivery` → 终态事件 → `settle_observations` → `ToolMessageMetadata.delivery` → `project_tool_result` 的 part `deliverables`；热投影与冷重建逐字段一致 | PY | `test_deliverable_record_projection.py`（新） | §4.1 | |
| S2-13 | 清除语义：终态事件不带记录时清掉 part 上的记录（与 `background` 相反）；驱动失败补写的 `cancelled` 后卡片消失，冷重建同样没有 | PY | 同上 | §4.1 | |
| S2-14 | 跟随：手动 fork 复制记录并展示；Agent fork 继承行不投影；撤回后不投影；删除随行删除 | PY | 同上 | §4.1 | |
| S2-15 | 契约文档第 3 节：`present_deliverables` 行写「无」，`deliverables` kind 从第 4 节移除；`display/deliverables_display.py` 删除后守卫仍绿 | PY | `test_tool_registry_display_guard.py`（不改，须通过） | §5 | |
| S2-16 | 提示词工具名守卫（新）：系统提示词里反引号包住的工具名都是已注册工具（现 `system_prompt_builder.py:517` 写死 `present_files`，没有测试能发现） | PY | `test_system_prompt_builder.py`（补） | §5 | |
| S2-17 | `FileInfo.width/height`：PNG、JPEG、GIF、WebP、AVIF、TIFF 读文件头得到原图尺寸；SVG、非图片为 null；像素超 Pillow 两倍上限的图片 `info` 仍 200、宽高为 null | PY | `test_workspace_files.py`（补） | §4.2 | |
| S2-18 | 资源 `edge`：位图返回边长不超过 `edge` 的缩小图；GIF 与动态 WebP 在字节预算内原样返回（字节相等）；非法 `edge`（0、负数、超上限）422 | PY | 同上 | §4.3 | |
| S2-19 | 缓存头：带版本且校验通过 → `private, max-age=31536000, immutable`；不带版本 → `no-store`；版本不符 409 不缓存；`X-Cosir-Image-Preview` 与其 CORS 暴露删除 | PY | 同上 | §4.3 | |
| S2-20 | 读取：`readDeliverables` 从 part 的 `deliverables` 读三种条目，丢弃形状不对的，不读 `display_data` | VT | `tests/unit/reply-presentation-display.test.ts`（改） | §4.1 | |
| S2-21 | 排版类别纯函数：`kind` × `system_preview` × 宿主能否给缩略图 → 方块 / 胶囊 / 音频；无宿主与 linux 下 PDF、Office、视频退回胶囊；位图与 SVG 恒为方块 | VT | `tests/unit/deliverable-layout.test.ts`（新） | §4.3 | |
| S2-22 | 回合聚合：一个回合多次交付调用合成一张卡，放在第一次调用的位置；按 `key` 只留最后一次；方块 → 胶囊 → 音频；失败的调用在过程里出调用行；未变化时复用旧卡片对象（同改动卡的 `unchanged`，`turn-layout.ts:162`） | VT | `tests/unit/turn-layout.test.ts`（改 `:127-134`） | §4.3、§11.1 | |
| S2-23 | 产物区顺序：改动卡在前，其余按调用顺序，交付物组在它所在位置；下一步建议在产物区之后 | VT | 同上 | §11.5 | |
| S2-24 | 估高：方块行数 + 胶囊按每行 3 枚算的行数 + 音频条数，n 个条目 > 0；条目从「12 KB」变「已不存在」「无法访问」「—」时估高不变；不测窄窗口精度（由虚拟列表实测修正） | VT | `tests/unit/message-height.test.ts`（补；改名后旧 `deliverableCount` 会静默变 0，`message-height-estimate.ts:113`） | §4.3 | |
| S2-25 | 条目组件：中间截断保住扩展名与结尾（无扩展名、`a.tar.gz`、超长 CJK）；角标大写后缀、目录与网址用图标；目录写「文件夹」、网址写主机名加外链箭头，主机名超长截断到大小格的固定宽度 | VT | `tests/unit/deliverable-item.test.tsx`（新） | §4.3、§11.1 | |
| S2-26 | 胶囊结构：主按钮与次按钮是并列的两个 `<button>`，不嵌套；Tab 先主后次；提示条两行（说明、完整路径：内为相对、外为绝对） | VT | 同上 | §4.3、§11.1 | |
| S2-27 | 现读：工作区内条目经 `fileInfoQueryOptions`，与文件面板同一 key（同一路径只发一次 `files/info`）；工作区外条目不发请求，显示记录的大小 | VT | 同上 | §4.1 | |
| S2-28 | 状态：404 `FILE_NOT_FOUND` → 虚线、划线、不可点、「已不存在」；403 → 同样的灰色虚线、「无法访问」、不可点，不用锁、不用警示色；422（解码失败、读取出错）、5xx、网络失败 → 外观照常、大小一格「—」、可点，点开由打开链路报错 | VT | 同上 | §11.2 | |
| S2-29 | 主操作去处（全部经 `use-open-link`，不另写解析）：工作区内文件 → 文件面板；`.html` 与本机地址 → 当前 Session 内置浏览器；工作区内目录 → 文件树展开（显式构造目录目标，`resolve-link.ts` 只认末尾 `/`），被浏览可见性隐藏的目录 → 在文件夹中显示；公网网址 → 确认后系统浏览器；工作区外文件与目录 → 在文件夹中显示；音视频文件与 `media` 非空的网址 → 确认后系统浏览器，永不进内置浏览器 | VT | 同上；`tests/unit/open-link.test.tsx`（补目录、媒体网址） | §4.3 | |
| S2-30 | 图片方块：位图 `src` 是带版本与 `edge`（方块边长两倍）的资源地址，灯箱用原图、方块用 `previewSrc`；SVG 方块与灯箱都用原始地址，无 `edge`、无 `previewSrc`；`onerror` 先失效一次文件信息，仍失败显示二进制空态；不传 `previewSrc` 的现有调用方行为不变 | VT | `tests/unit/image-lightbox.test.tsx`（新）、`image-read-view.test.ts`（保持） | §4.3 | 预览 |
| S2-31 | 监听登记表：按工作区分开引用计数，登记与释放成对；虚拟列表卸载卡片不改变会话级集合（集合取自 snapshot `deliverables`，不触发重连）；目录条目登记自身；工作区外不登记 | VT | `tests/unit/file-watch-store.test.ts`（新） | §4.8 | |
| S2-32 | 监听连接：`AppLayout` 为每个有登记的工作区各挂一个订阅（保活的会话页与面板属于不同工作区时两个）；面板关闭后会话登记的目录仍在监听；某工作区登记清零即关闭其订阅 | VT | `tests/unit/use-file-watch.test.tsx`（新；`useFileWatch` 现无单测） | §4.8 | |
| S2-33 | 监听的注册与路由（已完成，2026-11-16）：每个工作区只在解析后的根上注册一次递归监听，展开目录不再注册；最后一个订阅离开满 60 秒才释放，删除工作区立即释放且不影响其他工作区；事件按路径通知关心所在目录的订阅，`.cosir` 变化与根目录删除通知全部订阅。不断言原生投递时限 | PY | `test_workspace_files.py` 的 `test_watch_registers_each_workspace_once_and_releases_when_idle`、`test_watch_close_workspace_releases_registration_immediately`、`test_watch_routes_events_to_matching_subscriptions` | §4.8 | |
| S2-34 | E2E 点卡片：工作区 Markdown 进文件面板；目录在文件树展开并选中；本机网址调宿主 `browser.open`（假宿主记录）；公网网址出确认框；工作区外文件调 `files.reveal`（绝对路径） | PW | `tests/e2e/reply-presentation.spec.ts`（改） | §4.3 | |
| S2-35 | E2E 面板关着时文件被删：监听流（`page.route` 推 `changed`）使卡片变「已不存在」，没有打开面板 | PW | 同上 | §4.8 | |
| S2-36 | 浏览器开发模式（不装宿主）：没有次按钮；工作区外条目不可点、悬停仍有提示条 | PW | 同上 | §4.3、§11.2 | |
| S2-37 | 交付物与下一步建议同在：正文 → 交付物 → 建议；建议只在最后一条回复（现有用例保留） | PW | 同上 | §11.5 | |
| S2-38 | PW-R 真实后端：面板关着改写工作区文件，卡片大小更新；图片方块滚出再滚回，服务端没有第二次资源请求（夹具记请求数；开发期 Electron 关了缓存，打包表现见 M6）；会话没有工作区内交付物且面板收起时订阅数为 0，有交付物时为 1 且只含交付物父目录与根 | PW-R | `tests/e2e/deliverables-integration.spec.ts`（新） | §4.3、§4.8 | |
| S2-39 | 清单投影纯函数：条目形状 `{key, tool_call_id, index, turn, item}`；按 `key` 只留最后一次，新的在前；失败调用不计 | PY | `test_deliverables_projection.py`（新） | §4.6 | |
| S2-40 | 继承：手动 fork 计入；Agent fork 的继承行不在 | PY | 同上 | §4.6 | |
| S2-41 | 撤回：整份重建后被撤回回合的条目消失（热、冷一致，写法同 `test_background_task_projection.py` 的撤回用例） | PY | 同上 | §4.6 | |
| S2-42 | 重算时机：`apply_planned` 的事件指向交付物 part 时重算并发 `set deliverables`；失败补写清掉记录时也重算（不能挂在 `event.background is not None` 分支上，`conversation_state_service.py:250`）；token 帧不重算；`_refresh` 刷新后台字段不覆盖它 | PY | 同上 | §4.6 | |
| S2-43 | 历史窗口：完整快照只带最近一段消息时 `deliverables` 仍含窗口外条目 | PY | `test_snapshot_window.py`（补） | §4.6 | |
| S2-44 | 写入侧校验：字段集合与取值精确校验，坏条目使物化失败 | PY | `test_state_snapshot_write_side_validation.py`（补） | §4.6 | |
| S2-45 | HTTP / JSON schema 含 `deliverables`；前端校验接受它，拒绝缺字段与多余字段 | PY + VT | `test_snapshot_http_contract.py`（补）、`tests/unit/snapshot-validation.test.ts`（补） | §4.6 | |
| S2-46 | 夹具一次改完（G4 的 13 个文件），全部 PW 绿 | PW | 见 G4 | §5 阶段二 | |

### 1.3 阶段三：类型与预览（§4.2、§4.5、§4.10）

| ID | 验证什么与预期 | 层 | 文件 | 方案 | 兜底 |
|---|---|---|---|---|---|
| S3-01 | 判定表全量参数化：先锁现有六类（PDF、PNG、JPEG、GIF、BMP、WEBP、AVIF、ICO、SVG、Markdown、文本、二进制）不变，再加：`.csv`/`.tsv` 可解码 → table、否则 binary；`.mmd`/`.mermaid` 可解码 → mermaid；音视频表后缀且非文本 → audio / video；文本内容的 `.mp3`、TypeScript `.ts` 仍为 text | PY | `test_workspace_file_kinds.py`（新） | §4.2 | 类型 |
| S3-02 | `system_preview`：最终 kind 为 pdf / video 或后缀在第三张表；无 `.pdf` 后缀的 PDF 为真；`.key/.pages/.numbers`、`.heic` 为真（后者 kind 仍是 binary）；音频、压缩包为假 | PY | 同上 | §4.2 | |
| S3-03 | 主机无关：monkeypatch `mimetypes.guess_type` 返回 `None` 或乱值，kind 与 `system_preview` 不变（G2） | PY | 同上 | §4.2 | 类型 |
| S3-04 | 每个文件恰好一类：参数化夹具目录，kind 属于十类，可解码落 text、其余落 binary | PY | 同上 | §4.10 | 类型 |
| S3-05 | TIFF：`II*\0` / `MM\0*` → image；`.png` 后缀的 TIFF 按头识别；资源恒为 PNG（小图也转）；多页取首页；损坏 TIFF 与 DNG 转码失败返回 400 | PY | `test_workspace_files.py`（补） | §4.2 | 预览 |
| S3-06 | 资源白名单：audio、video 200 / 206，`Accept-Ranges`、`Content-Range`、`bytes=-N`、416、`If-Range` 不匹配退整文件；table、mermaid、text 仍 400 | PY | 同上（照 `test_resource_range_images_and_changed_version`） | §4.2 | |
| S3-07 | 音视频 MIME 取自后缀表：mimetypes 被 monkeypatch 成 `None` 或乱值时，`FileInfo.mime` 与资源 `Content-Type` 仍是表里的值（如 `.m4a` → `audio/mp4`），带 `nosniff`；其余类型的 `mime` 不变 | PY | 同上 | §4.2 | |
| S3-08 | 媒体不整文件读入：稀疏 64 MB 文件的 Range 只读所需字节（计数 `source.read`） | PY | 同上 | §4.2 | |
| S3-09 | 前端分发：`FileInfo["kind"]` 十类；table、mermaid 进可切换集合，audio、video 不可切源码 | VT | `tests/unit/file-preview-dispatch.test.tsx`（新） | §4.2 | |
| S3-10 | 统一降级：预览器上报「显示不了」→ 分发处换成二进制空态并写原因；读取失败（网络、404、403、5xx）仍走 `FileReadProblem`；太大只剩 SVG 413 切源码 | VT | 同上 | §4.10 | 预览 |
| S3-11 | 位图不分「无法识别」与「像素过大」：宽高为空的 image 不请求资源、直接二进制空态；`<img>` 出错先失效一次文件信息，仍失败进二进制空态（「无法预览这张图片」）；SVG 宽高为空不触发这条 | VT | 同上 | §4.2、§4.10 | 预览 |
| S3-12 | 表格解析：自动识别 `,` / `\t` / `;`；引号内逗号与换行；BOM；空行；列数不齐；只有表头；空文件 | VT | `tests/unit/table-preview.test.tsx`（新） | §4.5 | |
| S3-13 | 表格交互：升 → 降 → 还原；数字列右对齐等宽（千分位、负数、百分号、货币前缀的判定按实现写死）；筛选与「匹配数 / 总行数」；原始行号在筛选排序后不变 | VT | 同上 | §11.3 | |
| S3-14 | 表格超限：大于 2 MB 时只解析首段，截到最后一条完整记录（首段末尾正好落在引号内换行处的夹具）；上方一行「只显示前 N 行，文件共 X MB」与「以源码查看全部」 | VT | 同上 | §4.5 | |
| S3-15 | papaparse 致命错误 → 降级 | VT | 同上 | §4.10 | 预览 |
| S3-16 | mermaid 文件：内容作为一个 mermaid 代码块交给 Markdown 渲染组件；语法错误与 Markdown 内嵌 mermaid 表现一致，不白屏 | VT | `tests/unit/mermaid-file-preview.test.tsx`（新） | §4.2 | 预览 |
| S3-17 | 媒体预览：`src` 是带版本的资源地址；`error` 后先重读文件信息，文件在且版本未变才降级，版本变了按新版本重载，404 走读取问题 | VT | `tests/unit/media-preview.test.tsx`（新） | §4.10 | 预览 |
| S3-18 | `.ipynb` 按 JSON 高亮并格式化，只影响显示；JSON 损坏或分段阅读（> 2 MB）时显示原文 | VT | `tests/unit/code-language.test.ts`（新） | §5 阶段三、§8 #7 | 预览 |
| S3-19 | E2E 表格：真实后端 CSV / TSV，排序、筛选、行号、表头吸顶、横向滚动只在表格内（页面 `scrollWidth == clientWidth`）、切源码再切回 | PW-R | `tests/e2e/file-preview-kinds.spec.ts`（新） | §5 阶段三 | |
| S3-20 | E2E 音频：wav、ogg 点播放后 `currentTime` 增长；拖动进度触发 206 | PW-R | 同上 | §5 阶段三 | |
| S3-21 | E2E 视频：webm 可播放；内容不是媒体的 `.avi` 降级到二进制空态 | PW-R | 同上 | §4.10 | 预览 |
| S3-22 | E2E TIFF：文件面板显示转码后的图片，尺寸显示原图宽高；超大像素图直接显示二进制空态、没有资源请求 | PW-R | 同上 | §4.2 | |

### 1.4 阶段四：宿主三个方法、系统缩略图与出口（§4.3、§4.4）

| ID | 验证什么与预期 | 层 | 文件 | 方案 | 兜底 |
|---|---|---|---|---|---|
| S4-01 | 入参校验：三方法只收字符串绝对路径（≤ 4096）且存在；`thumbnail` 边长为有上限的正整数；不合法抛 `HostUserError` 中文原因（与 `parseRevealPath` 同口径）。`ipc.ts` 直接 import electron、现无任何单测，解析函数需提成纯模块才能测 | VT | `tests/unit/host-file-actions.test.ts`（新） | §4.4 | |
| S4-02 | `quickLook`：先显示并聚焦主窗口再调 `previewFile`；文件不存在不调用、按缺失返回；非 darwin 返回不可用 | VT | 同上（mock `BrowserWindow`） | §4.4、§8 #4 | |
| S4-03 | `openExternally` 不做后缀限制（`.command`、`.app` 放行）；`shell.openPath` 返回的错误串转成用户可见失败 | VT | 同上 | §4.4 | |
| S4-04 | `thumbnail` 仅 darwin、win32；linux 返回不可用 | VT | 同上 | §4.4 | |
| S4-05 | 契约：`host-contract.ts` 通道与类型、preload 暴露、`lib/desktop-host` 透传失败消息 | VT | `tests/unit/desktop-host.test.ts`（补） | §4.4 | |
| S4-06 | 缩略图门槛：只对 `system_preview` 为真的文件请求；先图标后换图；失败退回图标；无宿主 / linux 不请求 | VT | `tests/unit/deliverable-thumbnail.test.tsx`（新） | §4.3 | 预览 |
| S4-07 | 胶囊次操作菜单：阶段四起含快速查看（仅 darwin）、用系统应用打开、在文件夹中显示；无宿主没有菜单 | VT | `deliverable-item.test.tsx` | §4.3 | |
| S4-08 | 二进制空态两种说法（文案照抄 §4.4 表）：`system_preview` 真 → 「这种文件没有内置预览」，darwin 主按钮快速查看、win32 / linux 主按钮用系统应用打开；假 → 「这个文件不是文本，无法预览」，无快速查看；都有文字链接「以文本方式查看」 | VT | `tests/unit/binary-preview.test.tsx`（新） | §4.4、§11.3 | 预览 |
| S4-09 | 阅读工具栏「更多」：所有类型都有三个出口（快速查看仅 darwin）；无宿主不出现 | VT | `tests/unit/reading-toolbar-actions.test.tsx`（新） | §4.4 | |
| S4-10 | 假宿主扩展（G3）：`installFakeDesktopHost({ platform })`，三方法可录制 | PW（基础设施） | `tests/e2e/fake-desktop-host.ts` | §5 阶段四 | |
| S4-11 | E2E darwin：docx 空态显示假缩略图与三个按钮，点击后假宿主依次记录 `quickLook` / `openExternally` / `reveal`，参数为绝对路径 | PW | `tests/e2e/file-preview-kinds.spec.ts` | §4.4 | |
| S4-12 | E2E win32：无快速查看，「用系统应用打开」为主按钮 | PW | 同上 | §4.4 | |
| S4-13 | E2E 卡片：darwin 下 PDF / docx / 视频方块先占位再换假缩略图；`thumbnail` 抛错退回图标；linux 与无宿主退回胶囊 | PW | `reply-presentation.spec.ts` | §4.3、§11.2 | 预览 |

### 1.5 阶段五：侧栏「产出」卡（§4.6、§11.4）

| ID | 验证什么与预期 | 层 | 文件 | 方案 |
|---|---|---|---|---|
| S5-01 | 卡片注册：`CONVERSATION_ASIDE_CARD_IDS` 新增 id，每个 id 恰好一个实现；`useHasContent` 只在 snapshot `deliverables` 非空时为真；折叠摘要为数量 | VT | `tests/unit/conversation-aside.test.ts`（补）、`tests/unit/deliverables-aside-card.test.tsx`（新） | §4.6、§11.4 |
| S5-02 | 条目与回复卡共用组件：点击行为一致；侧栏不发 `files/info`、不划线；点击不存在的条目由打开链路提示「工作区里没有 …」 | VT | 同上 | §4.6、§11.4 |
| S5-03 | 偏好重置：旧的单卡偏好（order 长度 1）读入后整体回默认，含 `open`（`conversation-aside-store.ts:43`） | VT | `conversation-aside.test.ts` | §4.6 |
| S5-04 | E2E：只有交付物、没有后台工作时侧栏与页头入口出现；点条目进文件面板；折叠显示数量；两张真实卡片拖动换序后刷新保持（现在注册表只有一张卡，拖动排序从没有在两张卡之间测过） | PW | `tests/e2e/deliverables-aside.spec.ts`（新） | §4.6 |
| S5-05 | E2E：撤回含交付物的回合后条目消失；只剩被撤回的条目时整张卡消失 | PW | 同上 | §4.6 |

### 1.6 推迟

§4.9 工作区外的文件进文件面板：推迟，不测。

---

## 2. 回归测试

每行一条被波及的现有链路。「缺口」是现在没有测试、本次要补的；带 S 编号的已在 §1 列出。

### 2.1 阶段一：路径身份迁移（§4.7 全部调用点）

| # | 链路（代码） | 消费方 | 必须保持不变 | 守住它的已有测试 | 缺口与要补的测试 |
|---|---|---|---|---|---|
| R1 | 受保护路径 `core/permission/protected_paths.py:56`（`access`）、`:71`（`is_denied_root`） | `PathResolver` 读写入口、`file_walker.py:130` 剪枝、`writable_root.narrow` | `.env`、`models.json` 禁读写；`skills/` 只读；最深规则优先；随 `CODING_AGENT_DATA_DIR` 变；链接进 `.cosir` 被拒 | `test_protected_paths.py`（`:53-124` 全部） | 写法变体 S1-09、S1-11；`test_cosir_guard_probe_adversarial.py:79-90` 改写为「大小写不敏感卷上 `.COSIR` 属保留区」 |
| R2 | 写入与读取解析 `tools/tool_handler/security/path_resolver.py:138-150`、`:173-176` | `write_file`、`edit_file`、`apply_patch`、`read_file`、`read_image`、搜索、浏览器 `upload`（`browser/act.py:106`）、交付工具 | 相对路径按工作区根；读可出工作区、写不行；保留区与受保护路径拒；设备路径拒 | `test_path_resolver_cosir_guard.py`、`test_task3_canonical_write_paths.py`、`test_patch_round5_contract_adversarial.py:967`、`test_writable_root.py:58,84`、`test_read_file_paging.py`、`test_edit_file.py` | S1-12 |
| R3 | 可写根 `core/permission/writable_root.py:43-59` | 委派派生、`execute_terminal` 缺省工作目录、系统提示词 | 只能收窄；不存在 / 保护区拒；工作区根返回 None | `test_writable_root.py`（`:25-99`） | S1-13 |
| R4 | 原子写 `utils/atomic_write.py:115-118`、`:150-153`（手写祖先回溯） | 文件写工具、`tool_output_budget.py:276` 的产物、终端输出日志 | 越界 `EPERM`；失败不留临时文件；根是链接时等价 | `test_patch_round5_contract_adversarial.py:1065,1083`、`test_cosir_attachment_regression_adversarial.py:322,344` | S1-14 |
| R5 | 终端工作目录 `execute_terminal.py:626-638` | `execute_terminal` | 缺省为可写根；`..` 越界拒；不存在拒 | `test_execute_terminal_platform_contract_adversarial.py:798`、`test_execute_terminal_execution.py:135` | S1-15；`terminal_workbench_service.py:52` 只 resolve 工作区根做 cwd，随登记改存规范路径自然一致，不改 |
| R6 | 技能目录 `service/configuration/skill_catalog.py:177`、`:202-213` | `skill` 工具、配置页、提示词目录 | 链接出作用域的 Skill 被略过；资源越界拒；>1 MB 拒 | `test_mcp_skill_tools.py:122` | S1-16 |
| R7 | 附件 `service/attachment/attachment_service.py:164-169` | 上传、`read_image`、工具产出图片、浏览器截图 | 越界 `ATTACHMENT_NOT_OWNED`；根是链接时等价 | `test_cosir_attachment_regression_adversarial.py:164-279`、`test_attachment_dedup.py`、`test_read_image_tool.py`、`test_tool_image_channel.py` | S1-17；`is_trusted_cosir_path` 的用例（`:414-423`、`:656-663`）随函数删除 |
| R8 | 工作区登记 `service/workspace/workspace_service.py:122,191,194` | 全部读 `root_path` 的地方：文件服务 `root()`、终端 cwd、`browser/tab_context.py:44` 下载目录、`workspace_scope` 作用域键、监听 `file_watch.py:98` | 同一目录复用同一记录；非目录、相对路径拒；`.cosir` 初始化失败降级 | `test_workspace_service_create_reuse.py:70-178`（`:104` 依赖 casefold） | S1-18；`:104` 改为经规范路径通过 |
| R9 | 文件面板读取 `service/workspace_files/file_access.py:56,97`、`workspace_paths.py:26,37,58` | `/files/info|text|resource|directory|search`、监听订阅、`WorkspaceResourceResponse`、交付工具对工作区外文件的观察 | 句柄打开后校验范围；换链拒；FIFO 不挂；目录环标记；外指链接显示为 unavailable | `test_workspace_files.py:50,199,365`、`test_http_contract_and_resource_range`、PW-R `workspace-files-integration.spec.ts` 全部 | S1-19（现状红）、S1-20、S2-09 |
| R10 | 被删除的 `utils/path/validation.py` 与其测试 | — | — | `test_cosir_paths.py:40-77`、`test_cosir_guard_probe_adversarial.py` 大部分、`test_cosir_attachment_regression_adversarial.py:364-423` | 迁到 `test_file_identity.py`：同前缀兄弟、`./` 与 `..` 变体、NUL 与空串不抛、链接进出保留区，逐条保留语义 |
| R11 | 方案「不改的」四处：`workspace_scope.py`、`scope_config_directory.py:86`、`engine_cache.py:254`、`file_watch.py:98` | — | 行为不变 | 各自现有测试 | 不补测试；`file_watch.py:98` 的「工作区被移走」通知由 S1-18 的规范路径保证，W9 时顺带观察 |

### 2.2 阶段二至五

| # | 链路（代码） | 消费方 | 必须保持不变 | 守住它的已有测试 | 缺口与要补的测试 |
|---|---|---|---|---|---|
| R12 | 工具改名 `present_files` → `present_deliverables`（阶段二） | `tool_names.py:39,74`、`tool_models/__init__.py:17,49`、`tool_registration.py:29,68`、`schemas/__init__`、`define_agents`、`tool-catalog.ts:150`、`system_prompt_builder.py:517`、契约文档 `:56,82`、`test_session_tool_scope.py:25`、AGENTS kind 清单 | 只给侧栏会话；静默；`offer_next_step` 不受影响 | `test_tool_registry_display_guard.py`（目录覆盖、契约表全工具）、`test_session_tool_scope.py`、`test_reply_presentation_tools.py`、`tool-catalog.test.ts` | S2-16；交付检查（非测试）：`git grep present_files` 只剩文档历史 |
| R13 | 工具结果 metadata 的逐字段重建点（阶段二） | `runtime_message_context_manager.py:441-446`（显式构造，`delivery` 不写就丢）、`service/conversation/session_context_service.py:110`（`model_copy` 自动保留）、`core/tool/placeholder_settlement.py:85-93`、`tool_call_event_projection.py:140-147`、`core/tool/execute/tool_terminal_projection.py:190`、`node_helper/tool_observation_projection.py:55,109,145,157` | `display_data`、`background` 的现有合并语义不变 | `test_background_task_projection.py`、`test_final_reply_projection.py` | 每个重建点各一条：`delivery` 在该路径上是保留还是清除，与 §4.1 一致（方案把「去掉重复拷贝」列为刻意不做，所以每处都要测） |
| R14 | 回合布局、估高、产物区（阶段二） | `turn-layout.ts:14,128,160`、`artifact-zone.tsx:38`、`message-height-estimate.ts:111-114,158`、`turn.tsx` | 改动卡聚合与位置；独立产物；等用户决定期间不显示；渲染错误被 `RenderIsolationBoundary` 隔离 | `turn-layout.test.ts:100-134`、`message-height.test.ts`、`render-isolation.test.tsx`、PW `turn-render-isolation.spec.ts` | S2-22 至 S2-24；`STANDALONE_ARTIFACT_KINDS` 不再含 `deliverables`（`display_data` 为空，布局按 part 字段） |
| R15 | 文件监听提升（阶段二） | `workspace-files-surface.tsx:89-94`、`markdown-preview.tsx:25`、`resource-watch.tsx`（删除）、后端 `file_watch.py` | 原子替换实时更新与「文件已更新」；Markdown 相对图片更新；断线指数退避；端口换代取消旧连接；聚焦刷新；超过 128 个目录转轮询；无交付物且面板收起时订阅数为 0 | PW-R `workspace-files-integration.spec.ts:70`（原子替换）、`:115`（图片更新、监听释放，`:152-163` 仍成立）；PY 布线与路由测试（S2-33）；E2E 夹具写完文件后送合成事件，不等系统投递 | S2-31、S2-32、S2-38 |
| R16 | 图片地址与缓存（阶段二） | `image-preview.tsx:23,48,52`、`markdown-preview.tsx:29`、`resources.ts:16-58`（删除 `useImageResource`）、`query-keys.ts:15`（删除 `fileResource`）、`WorkspaceResourceResponse.py:36-50`、灯箱的全部调用方（读图、浏览器截图、用户消息图片） | 版本变化 409；PDF Range 与 `pdfResource` 不变；尺寸显示；灯箱翻页与失败占位 | PY `test_http_contract_and_resource_range`、`test_resource_range_images_and_changed_version`；PW `workspace-files.spec.ts:185`、PW-R `integration.spec.ts:115`（`naturalWidth` 20 → 40）、`message-images.spec.ts`、`fullscreen.spec.ts:59`；VT `image-read-view.test.ts` | S2-17 至 S2-19、S2-30；尺寸改读 `FileInfo` 后「大图缩略预览」由宽高比较得出；**有意改变**：图片解码失败从 `ErrorState` 改为二进制空态 |
| R17 | `FileInfo.kind` 六 → 十类（阶段三） | `file_content.py:65-99`、资源白名单 `workspace_files_api.py:128`、前端 `lib/api/workspace-files.ts`、`file-preview.tsx:62-90`、文件图标与链接图标 | 原六类逐个不变；`.md` 仍是 markdown；非音视频的 `mime` 不变 | `test_workspace_files.py`（判定表无系统覆盖） | S3-01 先锁基线；**有意改变**：二进制空态改版、PDF 失败改为降级，PR 说明列为行为变化 |
| R18 | snapshot 新增顶层字段（阶段二） | 后端 `conversation_state_snapshot.py`、`conversation_state_service.py:151,250,423`；前端 `contract.ts`、`snapshot-validation.ts`、`transport-frame-store.ts`；G4 的 13 个夹具 | 其余顶层字段刷新时机与帧内容不变；读取不重建只裁窗口 | `test_background_task_projection.py`、`test_state_snapshot_write_side_validation.py`、`test_snapshot_window.py`、`test_snapshot_http_contract.py`、`snapshot-validation.test.ts` | S2-39 至 S2-46 |
| R19 | 链接打开（阶段二） | `use-open-link.ts`、`resolve-link.ts`、`use-open-workspace-path.ts:76`；对话、Markdown、终端、工具卡片共用 | 行号定位；本机 HTML 进内置浏览器、无 Session 退回面板；公网先确认；迟到打开被丢弃；不存在给「查找同名文件」 | `open-link.test.tsx`（20 项）、`resolve-link.test.ts`（18 项）、PW `workspace-files.spec.ts:630,660`、`link-icons.spec.ts` | S2-29 |
| R20 | 「在文件夹中显示」（阶段二、四） | `ipc.ts:68-77`、交付卡、下载、后台任务 | 不存在给「文件已不存在」 | PW `reply-presentation.spec.ts` 第 1 条 | `parseRevealPath` 无单测，随 S4-01 补 |
| R21 | 会话侧栏注册表（阶段五） | `aside-card-registry.tsx`、`conversation-aside-store.ts:9,43`、页头入口 | 后台任务卡折叠、拖动、定位；无内容时侧栏与入口都不出现；宽窄两种摆放 | `conversation-aside.test.ts`、PW `conversation-aside.spec.ts`（13 条）、`background-surfaces.spec.ts`、`background-tasks.spec.ts` | S5-03、S5-04；`conversation-aside.spec.ts:267` 的前提补「也没有交付物」 |
| R22 | 文件面板分发与二进制空态（阶段三、四） | `file-preview.tsx:59-90`、`image-preview.tsx`、`pdf-preview.tsx`、`file-read-problem.tsx` | 带行号先切源码；每文件渲染 / 源码偏好；不存在的中性空态；窄宽度工具栏单行 | PW `workspace-files.spec.ts`（`:143-660`）、`file-read-problem.test.ts` | S3-09 至 S3-11、S4-08、S4-09 |

---

## 3. UI 视觉回归

### 3.1 现状

**仓库没有视觉回归能力**（2026-11-15 复查）：全仓无 `toHaveScreenshot` / `toMatchSnapshot`；`playwright.config.ts` 只有一个 `chromium` project，无 `snapshotPathTemplate` 与截图阈值；没有基线目录。现有 `page.screenshot({ path })`（如 `workspace-files.spec.ts:182,226,304,378`）只把图写进 `/private/tmp` 或 `test-results/` 供人看，不比对、不失败。

### 3.2 引入方案与代价

| 项 | 做法 |
|---|---|
| 机制 | Playwright 内置 `expect(locator).toHaveScreenshot()`，不加依赖 |
| 隔离 | `playwright.config.ts` 新增 project `visual`（`testMatch: /\.visual\.spec\.ts$/`），默认 project 用 `testIgnore` 排除；新增脚本 `test:e2e:visual` |
| 基线 | `snapshotPathTemplate: "tests/e2e/__screenshots__/{platform}/{testFilePath}/{arg}{ext}"`，入库 |
| 稳定性 | 固定视口 1280×800，窄宽度另录 720；`deviceScaleFactor: 1`、`animations: "disabled"`、`caret: "hide"`、`reducedMotion: "reduce"`；深浅色用 `emulateMedia({ colorScheme })`（`link-icons.spec.ts` 已有写法）；时长、时间 `mask`；`maxDiffPixelRatio: 0.01` |
| 素材 | 缩略图由假宿主返回固定 PNG；动图用静态 GIF（`animations: "disabled"` 停不住 GIF）；视频截图前暂停在 0 秒；音频不播放 |
| 平台 | 字体栈是系统字体（`globals.css`），跨系统像素不同：基线只在 macOS 录与比；设了 `COSIR_PLAYWRIGHT_EXECUTABLE` 时跳过 |
| 更新 | 有意改样式用 `--update-snapshots` 重录，差异随 PR 人工审图 |

**代价**：约 40 张基线，每张 20–150 KB，入库约 3 MB；每次样式改动要重录、审图；只在 macOS 有效，Windows 外观（`titleBarOverlay`、字体）仍靠 WIN-M；首次接入约半天到一天（配置、夹具、假宿主扩展与 G3 共用）。不引入的替代：保留截图写盘加人工看图，零维护、不防回归。

### 3.3 需要基线的界面与状态

规格以 §11 与同步后的稿为准。「深浅」列默认两张。

| ID | 界面 | 状态 | 依据 | 深浅 | 阶段 | 文件 |
|---|---|---|---|---|---|---|
| V1 | 胶囊组 | 默认：两个文件、目录、网址（稿 A） | §11.1 | 两张 | 二 | `tests/e2e/deliverables.visual.spec.ts` |
| V2 | 胶囊 | 悬停：底色变灰、次按钮出现在大小位置、提示条两行 | §11.1 | 两张 | 二 | 同上 |
| V3 | 胶囊 | 主按钮与次按钮各自的键盘焦点 `focus-ring` | §11.2 | 仅浅 | 二 | 同上 |
| V4 | 胶囊 | 超长中文名中间截断，结尾 `_v2.csv` 可见；超长主机名截断 | §11.1、§4.3 | 仅浅 | 二 | 同上 |
| V5 | 胶囊组 | 720 宽换行 | §11.1 | 两张 | 二 | 同上 |
| V6 | 胶囊 | 已不存在、无法访问（同为灰色虚线）、其他读取失败（「—」）、工作区外（与普通一致）（稿 C） | §11.2 | 两张 | 二 | 同上 |
| V7 | 胶囊 | 「12 KB」「已不存在」「无法访问」「—」四态宽度相同（比宽度，不比图） | §4.3 | — | 二 | 同上 |
| V8 | 方块行 | 两张图片方块与一个 SVG 方块（稿 B） | §11.1 | 两张 | 二 | 同上 |
| V9 | 组合 | 正文 → 交付物 → 下一步建议（稿 D） | §11.5 | 两张 | 二 | 同上 |
| V10 | 浏览器开发模式 | 无次按钮的胶囊；工作区外条目的不可点外观 | §11.2 | 仅浅 | 二 | 同上 |
| V11 | 音频播放条 | 未播放 | §11.1 | 两张 | 三 | 同上 |
| V12 | 方块行 | PDF、docx、视频（带播放标记）填假缩略图（稿 B） | §11.1 | 两张 | 四 | 同上 |
| V13 | 方块 | 缩略图加载中的占位（角标加文件名），尺寸与终态相同 | §11.2 | 仅浅 | 四 | 同上 |
| V14 | 胶囊 | 无系统缩略图（linux 假宿主）：PDF、Office、视频退回胶囊 | §11.2 | 仅浅 | 四 | 同上 |
| V15 | 胶囊 | 次操作菜单展开（darwin 三项、win32 两项） | §4.3 | 仅浅 | 四 | 同上 |
| V16 | 表格视图 | 默认（稿 2） | §11.3 | 两张 | 三 | `tests/e2e/file-preview.visual.spec.ts` |
| V17 | 表格视图 | 数字列降序、筛选后「38 / 1,240 行」、宽表横滚到中段 | §11.3 | 仅浅 | 三 | 同上 |
| V18 | 表格视图 | 超限提示行 | §11.3 | 仅浅 | 三 | 同上 |
| V19 | mermaid 文件 | 渲染视图 | §4.2 | 两张 | 三 | 同上 |
| V20 | 视频面板 | 黑底等比居中、原生控件（webm） | §11.3 | 两张 | 三 | 同上 |
| V21 | 音频面板 | 播放器 | §4.2 | 仅浅 | 三 | 同上 |
| V22 | 二进制空态 | 预览降级（播放器失败、位图无法预览，带原因） | §4.10 | 仅浅 | 三 | 同上 |
| V23 | 二进制空态 | 认得的文档，darwin：缩略图、主按钮快速查看（稿 3 左） | §11.3 | 两张 | 四 | 同上 |
| V24 | 二进制空态 | 认得的文档，win32：主按钮用系统应用打开 | §11.3 | 仅浅 | 四 | 同上 |
| V25 | 二进制空态 | 认不出的二进制：通用图标、两个按钮（稿 3 中） | §11.3 | 两张 | 四 | 同上 |
| V26 | 阅读工具栏 | 「更多」菜单展开 | §4.4 | 仅浅 | 四 | 同上 |
| V27 | 侧栏「产出」卡 | 展开，各类条目单行、无划线（稿 4） | §11.4 | 两张 | 五 | `tests/e2e/deliverables-aside.visual.spec.ts` |
| V28 | 侧栏「产出」卡 | 折叠显示数量 | §11.4 | 仅浅 | 五 | 同上 |
| V29 | 侧栏 | 窄视口浮层里与后台任务卡并列 | §11.4 | 仅浅 | 五 | 同上 |

稿本身不做基线（缩略图是占位画法），只供人工对照（M8）。

---

## 4. Windows 测试矩阵

**前置**：AGENTS 待办「启动与测试入口」先过（W0）；Windows 机器装好仓库依赖、后端 `.venv`、Playwright Chromium。现有 CI（`.github/workflows/terminal-subsystem.yml`）只在 `windows-latest` 跑 clippy，pytest / vitest 只在 ubuntu；建议新增一个 `windows-latest` job 跑 `test_file_identity.py` 与 `test_path_identity_call_sites.py`（GitHub 的 Windows 镜像是 NTFS，可建 junction、`subst`，不需要管理员）。

| ID | 前置条件 | 步骤 | 预期 | 方式 | 方案 |
|---|---|---|---|---|---|
| W0 | 全新 Windows 环境 | 运行 `scripts/start-desktop.ps1`、`npm run test:unit`、`npm run test:e2e`（无头） | 都能启动、跑完 | WIN-M（AGENTS 待办） | — |
| W1 | NTFS 卷上的工作区 | 跑 `test_file_identity.py` 的身份与包含用例 | 全绿；目录 `st_ino` 非 0 且两次 `stat` 相同 | WIN-A（CI） | §9 后清单 1 |
| W2 | ReFS、exFAT 卷，SMB 共享，`subst` 盘 | 在各卷建工作区，运行 W1 的同一组用例（参数化卷根，经环境变量传入） | `st_ino` 非 0 时全绿；为 0 时 `RootDirectory.at` 报错、工作区登记失败并给出明确原因 | ReFS / exFAT / SMB 为 WIN-M（需挂卷）；`subst` 为 WIN-A | 清单 1 |
| W3 | 系统 `.cosir` 在 `%USERPROFILE%` | 以 junction、目录符号链接（开发者模式）、8.3 短名（`GetShortPathNameW` 取得）、`\\?\C:\...`、小写盘符、`\\localhost\C$\...` 写法让 `read_file`、`write_file`、`search_content` 访问 `.cosir\.env` | 全部拒绝 | WIN-A（UNC 管理共享需管理员时退为 WIN-M） | 清单 2 |
| W4 | 两个硬链接指向同一文件 | 同时打开两个句柄取规范路径 | 记录返回的名字；身份相等、名字可不同（与 S1-08 一致，按残留接受） | WIN-A（只记录） | 清单 3 |
| W5 | `subst X: C:\proj`、映射网络盘上的工作区 | 用 `X:\` 登记工作区，交付 `X:\a.md`，看卡片与文件面板 | 规范路径还原成真实路径；卡片判为工作区内、面板能打开；记录显示路径是否符合预期 | WIN-M | 清单 4 |
| W6 | 工作区上级有系统 `.cosir` | 在 `%USERPROFILE%` 上级起 `search_content` / `find_files` | `.cosir` 被剪掉；`DirEntry.stat()` 的 `st_ino` 为 0 不影响（改用 `os.stat(entry.path)`） | WIN-A | 清单 5 |
| W7 | 目录选择框 | 先后以 `C:\Proj`、`c:\proj` 选同一目录登记 | 同一条记录 | WIN-M（原生对话框）；后端部分 WIN-A（直接调 `create_workspace`） | 清单 6 |
| W8 | 目录条目与工作区登记要取目录的规范路径 | 交付一个目录、以 8.3 短名与小写盘符登记一个工作区 | `canonical_path` 经 `os.path.realpath(strict=True)` 对目录成功，返回长名、真实大小写、无 `\\?\` 前缀；不调用 `os.open` | WIN-A | §4.7 |
| W9 | 应用开着、对话里有交付物（监听订阅存在） | 在资源管理器里重命名工作区文件夹及其父目录；再关掉面板、开一个没有交付物的会话后重试 | 记录有订阅时能否重命名；无登记时订阅关闭、可以重命名；结果写回 §4.8 决定是否去掉父目录监听 | WIN-M | 清单 7、§4.8 |
| W10 | 工作区有 PDF、docx、xlsx、pptx、mp4 | 看卡片方块与二进制空态的系统缩略图 | 真实内容、宽高比裁切铺满；记录首次与后续耗时 | WIN-M | 清单 8 |
| W11 | 交付 `.bat`、`.exe`、`.lnk` | 点「用系统应用打开」 | 记录实际表现（执行、弹 SmartScreen、无反应），写回 §4.4 与「安全与权限」待办 | WIN-M | 清单 8 |
| W12 | 交付 HEVC 的 mov、H.264 的 mp4 | 在面板里播放；分别在装与不装「HEVC 视频扩展」时 | 能播就播；播不了时降级到二进制空态，主按钮是「用系统应用打开」 | WIN-M | 清单 9 |
| W13 | 默认 Windows 注册表的 MIME 映射 | 读 wav、mp3、m4a、mp4、webm 的 `/files/info` 与资源响应头，并在面板播放 | `mime` 与 `Content-Type` 等于后缀表的值，与注册表无关；能播 | WIN-A（PY 读响应头，即 S3-07 在 Windows 上跑）+ WIN-M（播放） | §4.2 |
| W14 | 交付工作区外的 `C:\Users\x\Desktop\a.pdf` | 看胶囊提示条、点「在文件夹中显示」 | 绝对路径原样显示；资源管理器定位到文件 | WIN-M | §4.3 |
| W15 | win32 | 二进制空态与胶囊菜单 | 无快速查看，「用系统应用打开」为主按钮 | WIN-A（PW 假宿主 win32 即 S4-12）+ WIN-M（真宿主） | §4.4 |
| W16 | 终端输出里的 `C:\` 路径、对话里的 Windows 绝对路径链接 | 点链接 | 与交付物同一套打开规则（AGENTS「终端交互」中的 `C:\` 路径链接项） | WIN-M | §4.3 |
| W17 | Windows 上的会话（工作区在非系统盘） | 跑 `execute_terminal` 带大小写不同的 `workdir`、写入大小写不同的绝对路径 | 在根内的都接受 | WIN-A | §4.7 |
| W18 | 工作区注册存在（面板开着，或关掉后 60 秒内） | 在资源管理器里重命名、再删除工作区根目录 | 监听线程退出，订阅经 `check_health` 转轮询，面板显示工作区已不存在；再在空闲满 60 秒后重命名上级目录，不被拒 | WIN-M | §9 后清单第 7、10 项 |
| W19 | 系统开着最后访问时间更新（`fsutil behavior query disablelastaccess` 为 0 或 2） | 打开文件面板静置 1 分钟，观察 `files/watch` 推送的 `changed` 与 `files/info` 请求次数 | 静置时没有持续的 `changed`；出现循环则在事件处理里忽略纯访问时间变化 | WIN-M | §9 后清单第 11 项 |
| W20 | 未开开发者模式的普通账号 | 跑 `workspace-files-integration.spec.ts` | 夹具能启动；建软链接失败时整组被跳过并说明原因，而不是全部报错 | WIN-A | §9 后清单第 12 项 |

---

## 5. 人工实测（macOS）

| ID | 验证什么与预期 | 为什么不能自动化 | 方案 |
|---|---|---|---|
| M1 | 系统缩略图画质、宽高比裁切铺满、首次约 1 秒 / 后续 60–320 毫秒 | 要操作系统生成，假宿主只能给固定图；真实宿主会弹窗 | §8 #3 |
| M2 | 快速查看浮在主窗口之上并取得焦点；再次调用只换内容；主窗口在托盘时先唤起主窗口；文件不存在不弹空白面板 | `previewFile` 打开系统可见面板 | §8 #4 |
| M3 | 「用系统应用打开」：docx 用默认应用打开；`.command` 真的会执行（与 §4.4 风险说明一致） | 启动外部程序 | §4.4 |
| M4 | mov 原片、HEVC mov、H.264 mp4 在面板里能播、能拖动 | Playwright Chromium 无专有解码器（G1），桌面 E2E 会弹窗 | §8 #5 |
| M5 | 打包应用里交付桌面 / 文稿目录下的工作区外文件：授权弹框在 Agent 调用工具那一刻弹出（已接受的代价，确认确实如此）；允许后卡片正常；拒绝后工具点名该条目失败、其余条目照常 | 授权记在拉起后端的应用名下，开发期记在终端名下 | §4.7、§9、§8 #6 |
| M6 | 打包应用里图片方块滚出再滚回不重新下载（看后端访问日志） | 开发期渲染会话 `cache: false`（`electron/main/window.ts:70`），长期缓存只在打包应用生效 | §4.3、§9 |
| M7 | 动图（GIF、动态 WebP）在方块与灯箱里都会动 | 视觉基线停不住动画 | §8 #1 |
| M8 | 深浅色下与稿逐项对照（胶囊、方块、空态、表格、侧栏卡） | 稿是占位画法，不能做基线 | §11 |
| M9 | 本机 HTML 交付物在内置浏览器里相对样式与链接生效（对照 `benchmark/20`） | 需要真实内置浏览器 | §8 #8 |

---

## 6. 已定案（上一版的 Q1–Q23）

| # | 定案 | 方案落点 | 落到的测试 |
|---|---|---|---|
| Q1 | 包含判定先 `realpath` 再逐级比身份 | §4.7 原语表 | S1-03 |
| Q2 | 工作区外文件以所在目录为根调 `inspect_file` | §4.1 字段表 | S2-09 |
| Q3 | 403 一律中性「无法访问」，分错误码随 §4.9 | §11.2 | S2-28、V6 |
| Q4 | 侧栏不探测、不划线 | §4.6、§11.4 | S5-02、V27 |
| Q5 | 会话登记集合取自 snapshot `deliverables`，清单投影提前到阶段二；按工作区分订阅 | §4.8、§5 | S2-31、S2-32、S2-39 起 |
| Q6 | 周期失效只重拉已挂载查询，接受；无登记即关闭订阅 | §4.8 运行时成本 | S2-32、S2-38、W9 |
| Q7 | 位图不分两种失败，统一进空态；宽高为空不请求资源；SVG 413 仍切源码 | §4.2、§4.10 | S3-10、S3-11 |
| Q8 | 音视频 MIME 取自后缀表 | §4.2 | S3-07、W13 |
| Q9 | 估高按每行 3 枚胶囊，窄窗口由虚拟列表修正；主机名截断到定宽 | §4.3 | S2-24、S2-25、V7 |
| Q10 | 硬链接出两张卡，接受；§4.9 比对改为路径或身份任一相等 | §4.1、§4.7 残留、§4.9 | S1-08、W4 |
| Q11 | Windows 规范路径用 `realpath(strict=True)` | §4.7 原语表 | W8 |
| Q12 | 清单外三处列入「不改的」并写理由 | §4.7 | R11 |
| Q13 | 阶段一改写把绕过写成预期的用例 | §4.7 回归证明 | R1 |
| Q14 | 文件面板 403 缺陷列为阶段一回归证明 | §4.7 回归证明、§5 | S1-19 |
| Q15 | 浏览器开发模式下工作区外条目不可点，接受 | §4.3 | S2-36、V10 |
| Q16 | 其他读取失败写「—」、可点 | §11.2 | S2-28、V6 |
| Q17 | 交付受保护目录文件时弹授权框，接受 | §4.7、§9 | M5 |
| Q18 | 稿同步：去锁形、改「无法访问」、去「工作区外 · 只读」、侧栏不划线 | 稿 C、稿 3、稿 4 | V6、V23–V27 |
| Q19 | SVG 不带 `edge`、不设 `previewSrc` | §4.3 | S2-30 |
| Q20 | 网址 `key` 用 WHATWG 规范化后去片段 | §4.1 | S2-10 |
| Q21 | 阶段二开工前先修不稳定的原生监听用例 | §4.8 前置 | S2-33 |
| Q22 | `inspect_file` 捕获 `DecompressionBombError`，宽高置空 | §4.2 | S2-17、S3-22 |
| Q23 | 被隐藏的工作区目录主操作退回「在文件夹中显示」 | §4.3 | S2-29 |

---

## 7. 现状核对（2026-11-15，本机 macOS）

| 范围 | 结果 |
|---|---|
| 后端 pytest：本计划引用的 24 个文件（路径、受保护路径、可写根、原子写、终端工作目录、技能、附件、工作区登记、文件服务、交付工具、工具范围、契约守卫、后台看板、快照窗口与校验、HTTP 契约、提示词） | 481 通过、1 跳过、1 xfail、1 失败：`test_workspace_files.py::test_watch_native_update_and_release` 超时；单独重跑 3 次失败 2 次（S2-33） |
| 前端 Vitest：14 个文件 | 155 项全部通过 |
| Playwright E2E（无头，`COSIR_E2E_SLOT=7`）：`reply-presentation`、`workspace-files`、`workspace-files-integration`、`conversation-aside`、`message-images` | 41 项全部通过 |
| 脚本实测 | `is_within` 对 `.COSIR` 与 firmlink 写法返回 False（绕过成立）；工作区以 `PROJECT` 或 firmlink 写法为根时 `regular_file` 抛 `PermissionError`；F_GETPATH 能还原大小写、firmlink、链接，硬链接名不稳定；不先解析的祖先 stat 算法对链接与 `..` 逃逸误判；Pillow 对超大像素头 `open` 即抛错 |

## 8. 数量汇总

| 部分 | 项数 |
|---|---|
| 新功能测试（阶段一 21、二 46、三 22、四 13、五 5） | 107 |
| 回归链路（阶段一 11、其余 11） | 22 |
| 视觉基线条目（深浅合计约 40 张；阶段二 10、三 8、四 8、五 3） | 29 |
| Windows 用例（含 W0 前置） | 21，其中可自动化（含部分）11 |
| macOS 人工 | 9 |
| 已定案对照 | 23 |
