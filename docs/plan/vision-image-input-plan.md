# 方案计划：用户输入内联图像识别接入（Vision / 多模态）

> 状态：第 5 轮前修订（用户决策改为「复用 paths 路径引用、后端读文件」，替代原 base64 内联落库方案）
> 范围：仅「用户本地图片路径 → 模型视觉理解」最小闭环；暂不接工具产图、暂不接非 DeepSeek 厂商（但架构按可扩展设计）
> 决策依据：第零铁律（以长期稳定迭代为尺，敢改结构、敢引范式、不重复造轮子）；基于真实代码事实 + LangChain OpenAI 源码 + DeepSeek 官方文档

---

## 一、目标与边界

### 1.1 本期交付（最小闭环）
- 用户在轮次创建时提交**本地图片路径**（`paths` 参数，前端已落地：把图片保存在本地某路径下并传路径），
  **不传 base64**。后端在运行期从路径读文件、编码为 `image_url` content block，拼进 user 消息，
  经 ChatLiteLLM → DeepSeek `deepseek-v4-flash-vision-exp` 完成视觉理解。
- 图片本体留在本地文件系统，**DB 只存路径字符串**（`turns.paths` 已存在，零新增列、零新增表）。
  跨轮回放时后端从路径重新读文件重建 block；文件生命周期由用户本地保管（比临时落盘更稳，天然支持跨回放）。
- 模型不支持视觉、或图片违反官方尺寸/体积约束时，构建期或运行期拦截并给中文引导（不把 400 推给用户）。

### 1.2 明确不做（留给后续 PR，但架构预留）
- 工具产图（web_search / read_file 结果含图）打通。
- 非 DeepSeek 厂商接入（Anthropic/Gemini/DashScope 等）。但 JSON 声明层 + 厂商差异范式
  已按多厂商设计，新增厂商只需加 JSON 配置 + 一个 block 分支，不碰其它代码。

### 1.3 扩展性保证
- 图片输入格式差异（OpenAI `image_url` vs Anthropic `images` vs Gemini `inline_data`）
  收敛在单一模块 `vision_content_blocks.py`（类比 `thinking_extractor.py`），新增格式只加一个分支。
- 模型视觉能力（`supports_image`）与厂商视觉通道（`vision_input_format`）均声明于 JSON，
  由既有 `CapabilityService` 读取，不写死在业务逻辑里。

---

## 二、事实基础（代码 + 依赖源码 + 官方文档，已逐文件核验）

### 2.1 能力声明层已就绪（无需新建能力系统）
- `llm_provider/capability/model_capabilities.json` 已含 `supports_image` 字段，
  `deepseek-v4-flash-vision-exp` 已置 `true`。
- `llm_provider/capability/model_capability.py:101,148` 已建模 `supports_image: bool`，
  `ModelCapability.get_capability(model_name)` 已就绪（L108-152）。
- 结论：事实源已存在，本期只消费它，不新建能力系统。

### 2.2 厂商差异范式可复用（thinking_channel 完整链路）
真实链路（已读）：
`llm_provider.json` → `ProviderCapability`（字段 `thinking_channel`）
→ `CapabilityService.get_thinking_channel(provider_id)`（经 `get_provider_service().get_provider()`
+ `ProviderCapability.get_capability(provider.name)`）
→ `RuntimeConfig.thinking_channel`（`core/workflows/react/runtime_config.py`，含 `thinking_channel` 字段）
→ `ReactLikeWorkflow.run` 经 `config["configurable"]["runtime_config"]` 注入
→ `model_node` / `thinking_extractor` 按字段分支。
本期新增 `vision_input_format`，走同一条「JSON → ProviderCapability → CapabilityService →
RuntimeConfig → workflow 注入 → node helper 分支」链路（详见 3.5）。

### 2.3 LangChain OpenAI 多模态透传（零自研序列化，待本地 venv 逐行复核）
> 注：LangChain 源码**不在本仓库**（仅在运行时 venv 的 `site-packages`）。以下结论基于公开 LangChain
> OpenAI 行为 + 历史几轮审查时在本机 venv 的核验记忆，**行号需在实现前用本地 venv 实际打开
> `.../site-packages/langchain_openai/chat_models/base.py` 与 `langchain_core/messages/human.py`
> 逐行复核**，文档不保证行号精确。
- `_format_message_content` 对 `image_url` block（含 `data:image/...;base64,...`）走原生透传；
  对 Anthropic `image` block 自动转 `image_url`。
- `_convert_message_to_dict` 直接 `message.content` 透传；`human.py` `HumanMessage.__init__`
  原生支持 `content: list[dict]`。
- 结论：只需把 image block 放进 `HumanMessage(content=blocks)`，ChatLiteLLM 链路自动透传，
  **无需自定义序列化**。实现前必须逐行复核此结论（若 SDK 版本行为有变，需补最小适配层）。

### 2.4 DeepSeek Vision 官方约定（api-docs.deepseek.com/zh-cn/guides/vision）
- 仅 `deepseek-v4-flash-vision-exp` 支持图片；非视觉模型传图返回 400。
- 图片只能出现在 `user` 消息；`system`/`assistant` 带图 → 400。
- 单请求最多 600 张；格式 JPEG/PNG/GIF/WebP。
- **像素约束（请求侧硬约束，非平台自动处理）**：单边最长 8192px；当图片数量 ≥15 张时，
  单边须 ≤4096px，否则平台返回 400。
- **每张 token 上限 384**；**请求体总大小 ≤48MiB**。
- 传参格式（OpenAI 兼容）：
  ```json
  {"role":"user","content":[
    {"type":"text","text":"描述这张图"},
    {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}}
  ]}
  ```

### 2.5 当前架构约束（改造点，来自真实代码，行号已核验）
| 层 | 真实位置 | 现状 | 改造含义 |
|---|---|---|---|
| 消息值对象 | `models/runtime_message.py:12-33` | `RuntimeMessage(role, content_text:str, metadata:dict, frozen=True)` | 无 content block 数组 |
| 文本化核心 | `utils/message_content.py:17-49` | `content_to_text` 明确忽略 `image_url` 块 | 图像块会被静默丢弃（最危险点） |
| 落库转换 | `core/context/runtime_context_manager.py:434-475` | 落库强制 `content_to_text` | 图像进库即丢 |
| 回填转换 | `core/context/runtime_context_manager.py:477-516` | 回填 `HumanMessage(content=content_text)` | 图像出库即丢 |
| 非 str 兜底 | `core/context/runtime_context_manager.py:542-548` | 非 str content → 空格占位（仅 assistant） | user 块不受影响 |
| 用户消息入口 | `core/workflows/react/workflow.py:222-224` | `RuntimeMessage(role="user", content_text=turn.input_text)` | 用户图片无入口通道（workflow.py 仅此一处构造用户消息） |
| token 估算 | `models/runtime_message.py:35-64` + `utils/token_estimator.py` | 只估 `content_text`，零图像感知 | 需图像 token 分支 |
| turn 入口 | `api/schemas/request/CreateTurnRequest.py`（`paths` 字段 L36；`paths_validator` 即 `field_validator("paths")` L65-97） | `input_text` + `paths: list[str] | None`（注释已言可含图片路径；validator 已校验非空/空白/≤5 项/单条 ≤4096 字符） | **路径已能传、已能落库，唯一缺「运行期消费」**（注：`paths_validator` 是 `field_validator` 而非独立命名函数，文档统一称 paths_validator） |
| turn ORM | `storage/model/turn_model.py:67` | `paths: list[str]`（Text 列，已落库） | **已就绪，零新增列**；图片路径直接复用此字段 |
| 消息落库表 | `storage/model/turn_message_model.py:26-27` | `content_text: Text` + `metadata_json: Text` | 图像 block **仅运行时内存**，不落盘（从 paths 重建），不污染 metadata、不新增列 |
| 自动加列机制 | `storage/init_schema.py:_ensure_model_columns`（L124-182） | 比对 inspect.get_columns + ALTER TABLE ADD COLUMN | **本期零新增列，无需迁移** |

---

## 三、方案设计（分层改动，单一职责，不重复造轮子）

### 3.1 请求层：复用 `paths` 传图片路径（不传 base64）
文件：`api/schemas/request/CreateTurnRequest.py`

- **不新增 `attachments` 字段**。图片以「本地文件路径」形式经既有 `paths: list[str]` 传入
  （前端已把图片保存在本地路径下并传路径；`paths_validator` 已校验非空/空白/≤5 项/单条 ≤4096 字符）。
- 新增一个**语义校验**：当 `paths` 含图片扩展名（`.jpg/.jpeg/.png/.gif/.webp`，白名单）时，标记为待视觉处理；
  纯文本/目录路径仍按原有工具语义对待，互不影响。
- **校验时机调整**：路径合法性（非空、长度）在 `paths_validator` 已完成（请求期）；
  **文件存在性 + 图片格式 + 尺寸/体积**校验放在运行期 `vision_content_blocks` 读文件时做
  （见 3.6），因为文件可能在本轮创建后才落盘。视觉路径边界策略见 3.1.1（workspace 内
  `.cosir` 受信路径 vs 历史外部路径两条契约，均不做 workspace 写边界 `PathResolver` 硬拒）。
- 不引入 base64 预算（≤48MiB）请求期约束——文件在本地，体积由运行期读文件时校验即可。

#### 3.1.1 路径校验与依赖说明（视觉路径 ≠ 工具写路径）
> **契约边界（本期定稿）**：前端把用户图片落盘在 workspace 根目录下的 `.cosir` 元数据目录中
> （由 `workspace_service.create_workspace` 创建，幂等、失败降级），通过 `turn.paths` 传路径。
> 后端**放心读取 `.cosir` 内图片即可**，本次方案**不改动前端**。视觉路径校验因此分为两路契约：
> - **(a) 受信契约（本期主路径）**：图片位于 workspace 内的 `.cosir` 目录（路径前缀经
>   `workspace.root_path` 校验归属），后端可直接读，无需再做 workspace 写边界硬拒。
> - **(b) 历史/外部路径（兼容旧 turns）**：极少数旧数据可能含 workspace 外路径；按"文件存在 +
>   可读 + 格式白名单 + 体积上限"做存在性/安全校验，不做写工具式 `path_escape` 硬拒（否则误杀）。

- **不复用写工具的 workspace 边界**：`tools/handler/security/path_resolver.py` 的 `PathResolver.resolve`
  + `path_escape` 是为「工具写/读 workspace 内文件」设计的边界契约，会强制拒绝 workspace 外路径。
  视觉路径**(a) 受信 `.cosir` 路径**已在 workspace 内、无需复用其硬拒逻辑；**(b) 历史外部路径**
  则恰恰是要保留的合法输入，复用写边界会误杀。故视觉路径校验**独立设计**，仅做：
  文件存在 + 可读 + 扩展名/格式白名单 + 体积上限（≤48MiB 聚合，单图 ≤20MiB）。
  `.cosir` 受信路径的归属校验（路径是否以 `workspace.root_path/.cosir` 开头）在运行期
  `build_user_content_blocks` 读文件时执行（见 3.6）；该校验属于视觉层独立职责，不委托 `PathResolver`。
- **Pillow 依赖**：尺寸校验/真实格式探测需用 Pillow 解码。经核验 `pyproject.toml` 原无 Pillow；
  按第零铁律「敢引依赖、不重复造轮子」，本期**新增** `pillow==11.3.0`（锁定版本）。
  仅用于运行期读文件校验尺寸/探测真实格式，**不参与**多模态序列化（序列化由 LangChain 原生透传）。
  注：若后续确认 `web_content_store` 等已有图片处理依赖可复用，可降级为复用而非新增——实现时优先查重。

### 3.2 值对象层：RuntimeMessage 承载多模态（图片路径复用 turns.paths，不新增列）
- `TurnRecord`（`models/turn_record.py`）：**不新增字段**，直接复用既有 `paths: list[str]` 承载图片路径
  （`TurnRecord` 已有 `paths`，`from_model` 已透传）。
- `RuntimeMessage`（`models/runtime_message.py`，`frozen=True` dataclass）：
  新增可选字段 `content_blocks: list[dict] | None`（与 LangChain content block 同构）。
  - `content_text` 保留为纯文本回退（向后兼容旧落库数据）。（注：dataclass `frozen=True` 仅约束字段
    不可变，新增可选字段不影响冻结性，已核实。）
  - 当 `content_blocks` 存在时优先用于模型输入；`content_blocks` 为 None 时退回 `content_text`。
  - **`content_blocks` 仅运行时内存态**：由 `vision_content_blocks` 从 `turn.paths` 读文件后构造，
    不经过 `runtime_context_manager` 落库/回填（`_langraph_message_to_runtime_message` 不写它、
    `_runtime_message_to_langraph_message` 不读它），避免污染 `turn_messages` 表、零新增列。
  - `estimate_tokens()`（L35-64）增加图像分支：遍历 `content_blocks`（None 安全降级），
    文本块走 `TokenEstimator`，`image_url` 块调 `TokenEstimator.estimate_image`。

### 3.3 工具函数层（仅回退/测试用，非运行期主链路）
文件：`utils/message_content.py`（单一职责：content block 与文本互转）

> 明确归属：运行期 user 消息的多模态 block **唯一由 `vision_content_blocks.build_user_content_blocks` 从
> `turn.paths` 读文件构造**（见 3.2/3.6）。本节的 `content_to_blocks`/`blocks_to_human_message` **不是**
> 运行期主链路一环，仅用于：① 历史 content 文本的回退互转测试；② 未来若需把既有 `content_text`
> 反推为 block 的兼容场景。§4 改动清单将其列为「新增但主链路不调用」，以防被误读为 block 来源。

- 新增 `content_to_blocks(content) -> list[dict]`：返回完整 block 数组，**图像块不再丢弃**（回退/测试用）。
- `content_to_text` 语义明确为「纯文本回退」：文本块取 text，图像块降级为空串（仅用于旧库
  纯文本展示/日志摘要，不用于模型输入主路径）。
- 新增 `blocks_to_human_message(blocks) -> HumanMessage`：直接用 `HumanMessage(content=blocks)`（测试/辅助用）。

### 3.4 落库 / 回填层：图片路径落库、block 仅运行时（零新增列）
文件：`core/context/runtime_context_manager.py`（仅确认不改；图片路径走既有 `turns.paths`）

- **图片本体不进 `turn_messages` 表**：`content_blocks` 是运行期从 `turn.paths` 读文件后构造的内存字段，**不落库、不回填**。
- `_langraph_message_to_runtime_message`（L434-475）：**不改**，仍只写 `content_text`（文本回退），
  图像 block 不在此序列化。
- `_runtime_message_to_langraph_message`（L477-516）：**不改**，仍退回纯 `content_text`（向后兼容旧数据）。
- `_sanitize_assistant_messages`（L542-548）：仅作用于 assistant，user 块不受影响，无需改。
- **图片路径落库点**：`turns.paths`（`turn_model.py:67`，已存在）在 `create_turn` 时已写入
  （`turn_service.create_turn` L94 已下传 `paths`）。跨轮回放时：
  `load_history` → 读 `turn.paths` → `vision_content_blocks.build_user_content_blocks` 从路径读文件
  重建 block，无需任何 `turn_messages` 表改动、无需新增列、无需迁移。
- **注入点统一闭环（消除"当前轮/历史轮两个通道"的歧义）**：因 `content_blocks` 不落库，无论当前轮还是
  历史轮回放，图片 block **均在各自运行期经 `build_user_content_blocks(turn.paths)` 重建**，注入点唯一、无
  独立历史轮通道。历史轮与当前轮的区别仅在于 `turn` 对象来自 `get_current_turn()`（当前轮）还是
  `load_history`（历史轮），图片重建逻辑完全一致。
- **结论**：本期 `runtime_context_manager` / `turn_message_crud` / `turn_message_model` **三处均无需改动**，
  彻底消除第 3 轮"store 契约点错位"问题——因为根本不落盘图像 block。

### 3.5 厂商差异层：vision_input_format（四处处新增 + 完整注入链路）
真实路径与字段（已核验）：
- `llm_provider.json`（`llm_provider/capability/` 同级）：deepseek 条目新增 `"vision_input_format": "openai_url"`。
- `llm_provider/capability/provider_capability.py`：`ProviderCapability` 新增字段
  `vision_input_format: str = "openai_url"`。
- `llm_provider/provider/capability_service.py`：新增
  `get_vision_input_format(provider_id) -> str`（仿 `get_thinking_channel`，经
  `get_provider_service().get_provider()` + `ProviderCapability.get_capability(provider.name)`）。
- `core/workflows/react/runtime_config.py`：`RuntimeConfig` 新增 `vision_input_format: str` 字段，
  由 `ReactLikeWorkflow.run` 在构造 `RuntimeConfig` 时经 `CapabilityService.get_vision_input_format`
  填充（与 `thinking_channel` 同位置注入），并经 `config["configurable"]["runtime_config"]` 传给 node。
- **改造点作用域（消除注入链路歧义）**：用户消息构造发生在 `ReactLikeWorkflow.run` 方法内（与
  `runtime_config` 局部变量 L192 同一作用域），故 §3.6 直接读 `runtime_config.vision_input_format`
  为局部变量，无需二次取值；若后续把分支下沉到 `model_node` 等下游 node（跨作用域），则必须改为
  `config["configurable"]["runtime_config"].vision_input_format` 二次取值，与 §2.2 注入链路一致。
- `model_node` / `vision_content_blocks` 从 `runtime_config.vision_input_format` 读取并按格式分支。

### 3.6 模型输入层：构造多模态 user 消息
新建：`core/workflows/nodes/helper/vision_content_blocks.py`（纯函数集合，单一公开函数，命名具体不模糊）

- 公开函数：
  ```python
  def build_user_content_blocks(
      input_text: str,
      image_paths: list[str] | None,
      vision_input_format: str,
  ) -> list[dict]:
      """按厂商视觉格式把文本与本地图片路径拼成 content block 数组。

      职责单一：仅负责「(文本, 图片路径) -> content blocks」格式映射。
      流程（逐图隔离，单图失败不废整轮）：对每个 image_path ->
            路径归属校验（.cosir 受信路径 vs 历史外部路径，见 3.1.1，不复用写工具 PathResolver）->
            文件存在+可读校验 ->
            Pillow 打开探测真实格式与尺寸（mime 以真实格式为准，不信任扩展名；设 MAX_IMAGE_PIXELS 防解压炸弹）->
            尺寸校验（单边 8192px、≥15张 4096px；GIF 仅取首帧）-> 编码 data URI ->
            按 vision_input_format 拼 block；坏图收集进 skipped，不抛整轮异常。
      - "openai_url": {"type":"image_url","image_url":{"url":f"data:{real_mime};base64,{b64}"}}
      - "anthropic_images" / "gemini_inline": 本期抛 VisionFormatNotSupportedError（占位，待后续 PR）
      - 聚合体积 >48MiB（单图 ≤20MiB）：抛 VisionImageError（运行期，见 3.7）
      - 返回 (blocks, skipped: list[{"path","reason"}])，workflow 据 skipped 给中文引导
      """
  ```
- **`_is_image_ext` 归属（澄清未定义问题）**：该函数**定义在 `vision_content_blocks.py`**（与视觉逻辑同模块，
  单一职责），按扩展名白名单（`.jpg/.jpeg/.png/.gif/.webp`）判定；`workflow.py` **新增**从 `vision_content_blocks`
  导入使用（当前 workflow.py 无该导入，属本期新增，改动清单已标注），不在别处重复实现。
- **mime 以真实格式为准（欠考虑#4 修复）**：data URI 的 mime **不使用扩展名**，而由 Pillow
  `Image.open(path).format` 探测的真实格式映射（如 `.jpg` 实为 png 时取 `image/png`），避免声明与实际不符导致 DeepSeek 拒收。
- **解码安全（欠考虑补充：防解压炸弹 / DoS）**：读图前显式设定 `PIL.Image.MAX_IMAGE_PIXELS` 上限
  （如 200MP），超限 Pillow 抛 `DecompressionBombError`；解码异常（含炸弹/畸形格式）统一捕获为可降级
  错误 → 该图进 `skipped` 列表 + warning 日志，不阻断整轮。单图解码后若单边 >8192px 已在尺寸校验环节拦截，不进入编码。
- **GIF / 动图处理（欠考虑#8 收敛）**：本期**仅取首帧**——`Image.open(path).seek(0).convert("RGB")`
  后编码为单 block，多帧信息不保留；文档注明"动图按首帧处理"，多帧支持留待后续迭代（不在本方案范围）。
- **编码缓存与并发安全（欠考虑#2/#10 收敛）**：对 `(path, st_size, st_mtime)` 做模块级 LRU 缓存
  （`functools.lru_cache` 包一层或独立 dict），同轮/跨轮复用 base64 结果，避免每个 task 每轮重复读文件
  + 编码（数十 MB 级）。**并发结论**：桌面端 Python 后端为单进程、多 task 并发读同一图为只读、
  无写冲突，`lru_cache` 纯函数只读共享安全；若未来演进为 multiprocessing 后端，需改为进程级缓存或禁用 lru_cache。
- **`.cosir` 受信路径归属校验（契约 3.1.1 落地，路径归一化抗符号链接/大小写）**：归属判定**不依赖裸字符串前缀**
  （Windows 符号链接、NTFS 大小写保留会导致 `startswith` 误判），改用规范化比较：
  `norm_path = os.path.normcase(os.path.realpath(image_path))` 与
  `norm_cosir = os.path.normcase(os.path.realpath(os.path.join(workspace.root_path, ".cosir")))`，
  当 `norm_path == norm_cosir` 或 `norm_path.startswith(norm_cosir + os.sep)` 时视为受信路径直接读；
  否则按历史外部路径做存在性/格式/体积校验。两种路径均**不委托写工具 `PathResolver.path_escape`**（避免误杀 workspace 外历史图或重复边界）。
- **逐图失败隔离与日志级别（欠考虑#3 修复 + 回放噪音抑制）**：单图失败分两级处理——
  - **文件不存在（`file_not_found`）**：属预期失效（用户已删/移动图片、跨设备回放），记 **info** 级 + 进 `skipped`，
    **不记 warning**，避免历史轮频繁回放产生日志噪音（符合规范第六章"无边界循环日志/无效日志"）。
  - **非图片/尺寸超限/解码失败/体积超限**：记 **warning** 级 + 进 `skipped`（真正异常，需排查）。
  - 无论哪级均**不抛整轮异常**；仅当全部图片均失败时由 workflow 转中文引导。坏图在回复中以「N 张图已载入 / M 张未能载入（原因）」告知用户。
- 占位分支（非 `openai_url` 的 `vision_input_format`）统一抛 `VisionFormatNotSupportedError`
  （自定义异常，详见 3.7）。本期仅实现 `openai_url`，其它格式在**运行期**由 `build_user_content_blocks`
  抛 `VisionFormatNotSupportedError`，经 workflow 转 `VisionNotSupportedError`、API 层捕获转中文引导
  （见 3.7 捕获链），**非 `create_turn` 构建期捕获**——`create_turn` 仅做 `supports_image` 能力拦截，
  不感知 `vision_input_format` 取值（避免 §3.7 与运行期链路矛盾），避免运行时裸 `NotImplementedError`。
- `workflow.py:222-224` 改造为（**参数来源显式写出 + 异常捕获闭合**）：
  ```python
  # turn 来自 L142 operations.get_current_turn()，其 paths 字段由 create_turn
  # 经 turn_model.paths 落库回填而来（见 3.2，复用既有字段，零新增列）。
  image_paths = [p for p in (turn.paths or []) if _is_image_ext(p)]   # 仅抽图片路径（_is_image_ext 来自 vision_content_blocks）
  # vision_input_format 取自 ReactLikeWorkflow.run 内 L192 已构造的局部变量 runtime_config
  # （RuntimeConfig 经 CapabilityService.get_vision_input_format 填充，见 3.5；改造点与
  # runtime_config 同作用域，故直接读局部变量而非从 config["configurable"] 二次取值。若后续
  # 分支下沉到跨作用域的 model_node，则必须改为 config["configurable"]["runtime_config"] 取）。
  vision_input_format = runtime_config.vision_input_format
  try:
      blocks, skipped = build_user_content_blocks(input_text, image_paths, vision_input_format)
  except VisionFormatNotSupportedError as exc:
      # 厂商格式占位未实现：转中文引导，不裸 NotImplementedError。统一抛 VisionNotSupportedError
      # （与 §3.7 异常定义、API 层捕获三者同名，捕获链闭合）。
      raise VisionNotSupportedError(str(exc))   # 见 3.7 捕获层
  except VisionImageError as exc:
      # 聚合体积超限等运行期硬错：转中文引导，不阻断整轮（skipped 已含逐图软失败）。
      raise VisionNotSupportedError(str(exc))   # 同短码中文引导，由 API 层捕获
  RuntimeMessage(role="user", content_text=input_text, content_blocks=blocks)
  # skipped 非空时记录日志（见下方"逐图失败隔离"级别约定）并填充图片状态反馈
  ```
  （workflow.py 仅此一处构造用户消息，替换点精确；`image_paths`/`vision_input_format` 来源均显式绑定，不靠推断；
  `VisionFormatNotSupportedError` 与 `VisionImageError` 两类异常**均在 workflow 处捕获并转 `VisionNotSupportedError`**，
  与 §3.7 捕获链闭合，无遗漏分支。）

### 3.7 能力消费层：唯一构建期拦截点 + 异常定义
**唯一拦截点：`service/task/turn_service.py` 的 `create_turn`**（请求入口，最贴近用户、直接返回中文 400；
`model_factory.build_chat_model` 保持纯粹「模型名→ChatOpenAI」契约不变，不越权做请求语义校验）。

- **`create_turn` 入参无需扩展**：图片路径经既有 `paths: list[str]` 入参（L49 已存在）进入，
  `turns_api.py:105` 已下传 `paths=payload.paths`，`turn.create(...)`（L94）已落库 `paths`。
  **无需新增 `attachments` 入参**——这是相对原方案的最大简化。
- **拦截逻辑**（在 `create_turn` 内、模型名校验之后）：
  若 `paths` 含图片扩展名（见 3.1 白名单）且 `ModelCapability.get_capability(model_name).supports_image`
  为 False → 抛 `VisionNotSupportedError`（构建期拦截，不把 DeepSeek 400 推给用户）。
- **运行期校验**：文件存在性/尺寸/体积校验在 `build_user_content_blocks`（运行期、读文件时）做，
  失败抛 `VisionImageError`，由 `workflow.py` 捕获转中文引导（运行期，因文件可能创建后才落盘）。
- **异常定义（第 2 轮 A/F 项修正，均为自定义，不引用不存在的基类）**：
  新建 `app/core/llm_provider/exceptions.py`（或既有 exceptions 模块），定义三个独立异常，
  职责与捕获边界清晰区分：
  ```python
  class VisionNotSupportedError(ValueError):
      """当前模型不支持图像识别时由 create_turn 抛出（业务校验，构建期）。
      短码约定：[VISION_NOT_SUPPORTED] 请切换到支持视觉的模型（如 deepseek-v4-flash-vision-exp）。
      """
      pass

  class VisionFormatNotSupportedError(ValueError):
      """vision_input_format 非本期支持值时由 vision_content_blocks 抛出（扩展期/开发期）。
      短码约定：[VISION_FORMAT_UNSUPPORTED] 该厂商视觉输入格式暂未支持。
      """
      pass

  class VisionImageError(ValueError):
      """图片文件读取/校验失败（运行期，build_user_content_blocks 内）：
      文件不存在、非图片格式、尺寸超限、总体积 >48MiB。
      短码约定：[VISION_IMAGE_INVALID] <具体原因>。
      """
      pass
  ```
  - `VisionNotSupportedError`：**由 `create_turn` 抛出，由 API 层 `turns_api.py`（或全局异常处理器）捕获
    并转 HTTP 422/400 + 中文引导**——`create_turn` 本身只抛不捕，捕获职责在调用方（避免"自抛自捕"歧义）。
  - **捕获链闭合（关键，消除命名断裂）**：`workflow.py` `build_user_content_blocks` 调用处的
    `except VisionFormatNotSupportedError` 捕获后，**转抛 `VisionNotSupportedError`**（见 3.6 代码段
    `raise VisionNotSupportedError(str(exc))`）。即运行期格式未支持 → 统一上抛 `VisionNotSupportedError`
    → 由 API 层与构建期 `create_turn` 抛出者**同名捕获**，转同一中文引导「该厂商视觉格式暂未支持」。
    三处（§3.7 定义 `VisionNotSupportedError`、§3.6 转抛 `VisionNotSupportedError`、API 层捕获
    `VisionNotSupportedError`）名称完全一致，捕获链闭合，无 `VisionNotSupportedGuidance` 这类未定义类型。
  - `VisionImageError`（聚合体积超限等运行期硬错）：由 `workflow.py` `build_user_content_blocks` 调用处
    `except VisionImageError` 捕获后，**同样转抛 `VisionNotSupportedError`**（与 §3.6 代码段第二 `except`
    分支一致），由 API 层统一中文引导。即 `VisionImageError` 与 `VisionFormatNotSupportedError` 在 workflow
    处都归一为 `VisionNotSupportedError` 上抛，**短码/文案统一**，不保留两类各异的引导（避免捕获链分支发散）。
    **注意**：单图软失败（skipped 列表，如文件不存在/非图片）走的是 `skipped` 非异常路径，不在此捕获链内。
  - **逐图失败（skipped 列表，非异常）**：不抛异常，由 `build_user_content_blocks` 返回 `skipped`，
    workflow 记 warning 日志并在回复中告知「M 张未能载入（原因）」，不阻断整轮。
- 拦截在构建期（模型能力，API 层转 HTTP）+ 运行期（文件校验，workflow 捕获转引导）双层，不把 DeepSeek 400 推给用户。

### 3.8 token 估算扩展
文件：`utils/token_estimator.py`

- 新增 `estimate_image(image: dict) -> int`：按厂商上限估算。**用途边界**：仅用于本地上下文占用
  预算告警（与 `ContextUsagePayload` 同源，基于 `RuntimeContext.messages` 本地估算，不依赖模型实际计），
  **不影响请求本身**。DeepSeek 每张消耗 token 上限为 384（官方硬上限）；若 `llm_provider.json` 有
  `image_token_per_unit` 配置则读取，默认 384。
- `RuntimeMessage.estimate_tokens` 调用它汇总（文本 + 图像）。
- **回放不变量（欠考虑#7 收敛，机制保障而非口头承诺）**：token 估算**统一在编码层产出**，不两处各自估算。
  `build_user_content_blocks` 在返回 `blocks` 的同时，按 `estimate_image`（基于 base64 长度 / 固定系数
  或官方 384 上限）返回每图估算 token；`current turn` 与 `load_history` 回放均经同一函数重建 block，
  故两者 token 估算必然一致。实现时必须保证「block 重建函数」即「token 估算数据源」单一收口，禁止
  workflow 与 token_estimator 各算一遍（§3.6 已注明编码缓存键 `(path, st_size, st_mtime)`，估算同源）。
- **前端图片状态反馈（欠考虑#6 收敛）**：本期不新增结构化状态字段（不破坏 TurnRecord 列约束）；图片成败
  经 workflow 文字回复告知（skipped 列表）。前端如需高亮，后续可扩展 `turns` 状态字段或在 SSE 增加
  独立 vision 状态事件——列为后续迭代，不在本方案范围，但 §3.9 已禁止把本地路径/base64 写进任何 payload。

### 3.9 SSE / 回放字段裁剪（防 base64 泄露，规范第六章）
**真实序列化泄露面（第 2 轮 D 项修正，已核验）**：
- SSE 事件经 `RuntimeEvent.to_dict`（`models/event/runtime_event.py:88-114`）→ `payload.model_dump(...)`，
  序列化为 JSON 流。所有运行时事件内容来自 `models/payload/*.py` 的 payload 模型。
- 轮次查询响应经 `TurnResponse.from_record`（`api/schemas/response/TurnResponse.py:44-74`），
  当前不映射 `paths`（字段未列出），前端查询天然不暴露图像本地路径/base64——本期本就不新增 `attachments`
  字段（见 §3.1），故"不映射 attachments"的论证前提作废，改以"不映射 paths"为据。
- `RuntimeMessage`（`models/runtime_message.py`）**无 `to_dict` 方法**（仅有 `estimate_tokens`），
  不作前端序列化出口，仅作落库/回填内部值对象。

**约束规则**：
- **严禁**把 `RuntimeMessage.content_blocks`（含 `data:` URI）或 `TurnRecord.paths` 中的图片本地路径写入任何
  `RuntimeEventPayload`（无论当前是否有「用户消息回显」事件，未来扩展也禁止——一旦需要用户消息回显，
  必须仅回显 `content_text` 文本与"图片 x N"的占位描述，绝不能带 image block 或本地绝对路径）。
- `TurnResponse` 保持不暴露 `paths`（前端查询本就不含完整路径语义；如需展示可仅返回文件名）。
- 图像路径仅存于 `turns.paths`（本地路径字符串），图像字节仅在后端运行期「读文件 → 编码 block → 模型输入」链路存在，
  不向前端序列化、不经 SSE 泄露。
- 日志：图片接收只记「数量/纯文件名(basename，剥离父目录)/体积/mime」，不记 base64 原文，不记完整绝对路径
  （含用户名等敏感片段），符合规范第六章敏感信息保护。**无论图片位于 `.cosir` 还是其它受信目录，日志一律
  仅记 basename，绝不记录绝对路径或 workspace 根前缀**（`.cosir` 路径含 workspace 根，可能含用户名）。

---

## 四、改动文件清单（聚焦、单一职责）

| 文件 | 改动 | 职责 |
|---|---|---|
| `api/schemas/request/CreateTurnRequest.py` | 仅扩展 `paths_validator` 语义：识别图片扩展名标记，不新增字段、不新增列 | 请求入口校验 |
| `models/runtime_message.py` | 加 `content_blocks` 内存字段 + 图像 token 分支（`estimate_tokens`） | 消息值对象 |
| `utils/message_content.py` | 加 `content_to_blocks` / `blocks_to_human_message` | 文本↔block 互转 |
| `utils/token_estimator.py` | 加 `estimate_image` | token 估算 |
| `service/task/turn_service.py`（create_turn 拦截） | **不扩展入参**（`paths` 已存在）；加构建期模型能力校验/拦截 | 服务层 |
| `core/workflows/react/workflow.py` | 用户消息改用 block 构造（L222-224）：**新增**从 `vision_content_blocks` 导入 `build_user_content_blocks` 与 `_is_image_ext`；从 `turn.paths` 抽图片路径 → `build_user_content_blocks` | 工作流编排 |
| `core/workflows/nodes/helper/vision_content_blocks.py`（新） | 读 `image_paths` → `.cosir` 受信路径归属校验（不复用写工具 PathResolver）→ 读文件 → Pillow 尺寸/解码炸弹校验（MAX_IMAGE_PIXELS、GIF 首帧）→ 编码 data URI → 按 `vision_input_format` 拼 block（仅 `openai_url`，其余抛 `VisionFormatNotSupportedError` 由 workflow 转 `VisionNotSupportedError`；文件失败抛 `VisionImageError`；逐图失败进 skipped） | 视觉输入构造 |
| `llm_provider/capability/provider_capability.py` | 加 `vision_input_format` 字段 | 厂商能力 |
| `llm_provider/provider/capability_service.py` | 加 `get_vision_input_format` | 能力读取 |
| `core/workflows/react/runtime_config.py` | 注入 `vision_input_format` | 运行时配置 |
| `llm_provider.json`（provider 配置） | 加 `vision_input_format` | 厂商声明 |
| `app/core/llm_provider/exceptions.py`（新或并入既有） | `VisionNotSupportedError` / `VisionFormatNotSupportedError` / `VisionImageError`（均 `ValueError` 子类） | 异常 |
| `apps/backend/pyproject.toml` | 新增 `pillow==11.3.0`（锁定版本，仅运行期读文件校验尺寸/防解压炸弹） | 依赖 |

> 不改动：`turn_model.py`（复用既有 `paths` 列）；`turn_message_model.py`（图像 block 不落盘，零新增列）；
> `runtime_context_manager.py`（图像 block 仅运行期内存，不落库/回填）；`turn_message_crud.py`（无落库契约改动）；
> LangChain / litellm 源码（透传已原生支持）；`model_factory.py`（保持纯粹构造契约）；
> `TurnResponse.from_record`（天然不暴露 paths 完整内容，无需改）。

---

## 五、测试与日志（符合规范第六、八章）

- 单元测试（pytest）：
  - `vision_content_blocks`：openai_url 格式正确性（data URI 编码）；非支持格式抛 `VisionFormatNotSupportedError`；
    文件不存在/非图片/尺寸超限/总体积 >48MiB/解码炸弹抛 `VisionImageError`；`.cosir` 受信路径归属校验正确；
    **视觉路径不复用写工具 `PathResolver`**（验证 `build_user_content_blocks` 不依赖 `PathResolver`，外部路径不抛 PathResolver 错误）。
  - `message_content`：`content_to_blocks` 保留图像块；`content_to_text` 不丢文本。
  - `runtime_context_manager`：确认带 `content_blocks` 的 user 消息**不落盘**、后端从 `turn.paths` 重建一致（不丢图像引用）。
  - `token_estimator.estimate_image`：DeepSeek 384 上限。
  - `create_turn`：非视觉模型 + 图片路径 → `VisionNotSupportedError`；图片路径校验（扩展名白名单）。
  - `paths_validator` 扩展：图片扩展名识别、原非空/空白/≤5 项/单条 ≤4096 字符约束仍生效。
- 日志：图片接收（数量/文件名/体积/mime，不记 base64、不记完整绝对路径）、模型不匹配拦截均写 error 日志。
- 闭环：开发完成后启动独立审查 Agent + 独立测试 Agent，审查不通过则修后再审，至少三轮。

---

## 六、风险与开放问题（修订为 paths 引用策略后）

1. **路径失效（可接受权衡）**：图片本体留在用户本地（本期契约：落于 workspace 内 `.cosir` 目录），
   DB 仅存路径字符串。若用户删除/移动原图或跨设备回放，路径失效 → 运行期 `build_user_content_blocks`
   抛 `VisionImageError`（文件不存在）或进 `skipped` 列表，由 workflow 转中文引导（逐图隔离，不废整轮）。
   这是"本地一体化 agent"可接受的设计权衡。`.cosir` 文件生命周期与会话绑定：删除 workspace 时由
   `delete_workspace` 级联清理（含 `.cosir`），符合预期；单图删除不级联改 `turns.paths`（仅运行期跳过）。
2. **SSE 泄露**：已明确 3.9 真实泄露面（`RuntimeEvent.to_dict` 与 `TurnResponse.from_record`），
   并约定图像 data URI 与本地路径绝不进任何 `RuntimeEventPayload`、`TurnResponse` 不暴露完整路径。
3. **后续厂商**：新增厂商仅改 JSON（加 `vision_input_format` + `models` 的 `supports_image`），
   并在 `vision_content_blocks` 加一个分支，不碰其它代码。
4. **Pixel 校验依赖**：尺寸/格式探测用 Pillow（第 2 轮审查确认 pyproject 原无此依赖），本期新增
   `pillow==11.3.0`（锁定版本），符合第零铁律「敢引依赖、不重复造轮子」；仅用于运行期读文件取尺寸/真实格式。
5. **DB 体积（已消除）**：因不落盘图像 block、DB 仅存路径字符串，原 base64 撑爆 DB 风险彻底消除。
6. **已知开放点（已实现细节消化，均已收敛为明确结论，不阻塞方案成立）**：
   - **paths 分流契约（#5）**：`paths` 同时承载视觉图与工具上下文文件，本期按 `_is_image_ext` 扩展名分流
     （图像进视觉管线、其余保持原工具语义），未引入结构化 `images`/`files` 分离；若歧义显现再重构。
   - **前端图片状态反馈（#6，已收敛）**：详见 §3.7 末段——本期不新增结构化状态字段（不破坏 TurnRecord 列约束），
     靠 workflow 文字回复（`skipped` 列表）告知；前端高亮留待后续迭代（SSE 独立 vision 状态事件），但 §3.9
     已禁止把本地路径/base64 写进任何 payload。
   - **token 估算回放绑定（#7，已收敛）**：详见 §3.8——token 估算统一在编码层产出（block 重建函数即数据源单一收口），
     current turn 与 load_history 回放均经同一函数，必然一致，机制保障而非口头承诺。
   - **GIF/动图（#8，已收敛）**：详见 §3.6——本期仅取首帧编码，多帧不保留，多帧支持留后续迭代。
   - **并发只读（#10，已收敛）**：详见 §3.6——桌面端 Python 后端单进程，多 task 并发读同一图为只读、
     `lru_cache` 纯函数共享安全；multiprocessing 演进时需改进程级缓存。
   - **`.cosir` 被基础设施误处理（新增，已收敛）**：`.cosir` 为 workspace 内受信元数据目录，须在整体生命周期中显式排除——
     CodeGraph 索引、文件快照（file_snapshot_hook）、变更集（change_set）等机制**须显式跳过 `.cosir` 目录**，
     既不索引也不快照其内部图片（避免把用户图片纳入代码索引/撤销粒度）；`delete_workspace` 级联删除 `.cosir`
     是符合预期的行为（风险1 已注明）。该排除规则在「后续实现」时落到对应 hook / 索引服务的路径过滤配置。
