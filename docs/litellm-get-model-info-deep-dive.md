# litellm `get_model_info` 深度调研

> **调研对象**：`apps/backend/.venv/Lib/site-packages/litellm/utils.py`
> **版本**：`litellm-1.97.0.dist-info/METADATA` 声明 `Version: 1.97.0`
> **调研日期**：2026-08-25
> **调研方式**：直接阅读本地 venv 内 litellm 源码（非官方文档、非网络检索）

本文档记录 litellm 解析「模型元信息」（上下文窗口、定价、能力开关）的完整链路，
用于支撑本项目 `core/llm/context_window_resolver` 与 `ModelCatalog` 相关设计决策。

---

## 一、结论速览

`get_model_info` 的本质是**一次「模型名 → 静态成本映射表条目」的多候选名分层查找**，
外加两条旁路（provider 动态查询、正则泛化规则）和两层 LRU 缓存。

关键事实（均可在源码定位）：

| 事实 | 源码位置 |
| --- | --- |
| 数据底座是 `litellm.model_cost` 这个进程级全局 dict | `litellm/__init__.py:523` |
| 该 dict 默认**从 GitHub 远程 URL 拉取** | `litellm/__init__.py:405-408` |
| 公共入口只有 4 个参数，且 `api_key` 不参与缓存键 | `utils.py:5659-5739` |
| 真正的查找逻辑在 `_get_model_info_helper` | `utils.py:5274-5620` |
| 候选名共 5 个，按特异性从高到低依次尝试 | `utils.py:5367-5415` |
| 查不到时抛 `Exception`（不是返回 None） | `utils.py:5618-5620` |
| 两层 `lru_cache`，默认 maxsize=64 | `utils.py:5223, 5650`；`constants.py:342` |

---

## 二、数据底座：`litellm.model_cost` 是怎么来的

### 2.1 装载入口

`litellm/__init__.py` 在 import 期即完成装载：

```python
# litellm/__init__.py:521-523
from litellm.litellm_core_utils.get_model_cost_map import get_model_cost_map

model_cost = get_model_cost_map(url=model_cost_map_url)
```

`model_cost_map_url` 默认指向 GitHub main 分支的 JSON：

```python
# litellm/__init__.py:405-408
model_cost_map_url: str = os.getenv(
    "LITELLM_MODEL_COST_MAP_URL",
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json",
)
```

> **这是一个对本项目有实质影响的事实**：只要 `import litellm`，
> 默认行为就是发起一次**网络请求**去拉 GitHub 上的价格表。

### 2.2 `get_model_cost_map` 的三条路径

`litellm_core_utils/get_model_cost_map.py:426-478` 定义了装载决策：

1. **强制本地**：环境变量 `LITELLM_LOCAL_MODEL_COST_MAP=true` → 只读本地备份 JSON，
   记 `source="local"`、`is_env_forced=True`，直接返回（不发网络请求）。
2. **远程拉取**：调用 `GetModelCostMap.fetch_remote_model_cost_map(url)`（`timeout` 默认 5 秒）。
   - 抛异常 → 记 `fallback_reason=f"Remote fetch failed: {e}"`，降级本地备份。
3. **完整性校验**：远程拿到后过 `validate_model_cost_map(fetched_map, backup_model_count)`。
   校验不过 → 记 `fallback_reason="Remote data failed integrity validation"`，降级本地备份。

校验之所以存在，是为了防止远程返回一个「结构合法但条目数暴跌」的坏表。
注意 `_get_backup_model_count()` 只缓存一个 int，本地完整 dict 仅在真的需要
作为 fallback 返回时才解析——源码注释明确说明这是为了不长期占内存
（`get_model_cost_map.py:434-436`）。

**对本项目的启示**：若要杜绝启动期网络依赖 / 保证构建可复现，
应显式设置 `LITELLM_LOCAL_MODEL_COST_MAP=true`。

### 2.3 装载后的两步后处理：`_finalize_model_cost_map`

`get_model_cost_map.py:414-423`。三条路径的返回值都过这个函数：

```python
def _finalize_model_cost_map(model_cost: dict) -> dict:
    raw = model_cost.pop(FALLBACK_GENERALIZATIONS_KEY, None)
    rules = raw.get("rules") if isinstance(raw, dict) else None
    set_fallback_generalizations(rules)
    return _expand_model_aliases(model_cost)
```

1. **摘出泛化规则**：把 JSON 里的 `fallback_generalizations` 块 `pop` 出来，
   装进泛化规则模块（见第五节），**并从 map 中移除**，
   避免它被当成一个名叫 `fallback_generalizations` 的「模型」。
2. **展开别名**：`_expand_model_aliases`（`:359-411`）把条目内的 `aliases` 列表
   提升为顶层 key。关键实现细节——**别名指向同一个 dict 对象**
   （源码注释 `# same dict reference`，`:404`），所以零内存开销；
   随后 `aliases` key 被 pop 掉，下游永远看不到它（`:406-408`）。
   别名与已有 canonical 条目冲突 / 被其他条目抢占时，**跳过并 warning**（`:388-403`）。

---

## 三、公共入口 `get_model_info`

```python
# utils.py:5659-5739（省略长 docstring）
def get_model_info(
    model: str,
    custom_llm_provider: str | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
) -> ModelInfo:
    # api_key is a per-caller credential, not part of the model identity, so it is
    # kept out of the cache key; explicit keys are resolved without the cache.
    if api_key is not None:
        return _build_model_info(model, custom_llm_provider, api_base, api_key)
    return _cached_get_model_info(model, custom_llm_provider, api_base)


get_model_info.cache_clear = _cached_get_model_info.cache_clear
get_model_info.cache_info = _cached_get_model_info.cache_info
```

三个要点：

- **`api_key` 是缓存的旁路开关**。传了 `api_key` 就**完全绕过 LRU**，
  因为 key 属于「调用方凭证」而非「模型身份」，不能进缓存键（源码注释原话）。
- 未传 `api_key` 时走 `_cached_get_model_info`，缓存键为 `(model, custom_llm_provider, api_base)`。
- 函数对象上**挂了两个属性** `cache_clear` / `cache_info`（`:5742-5743`），
  这是 litellm 对外暴露的缓存操作面——可用于测试或热更新后手动清缓存。

### 3.1 `_build_model_info`：拼装最终 `ModelInfo`

```python
# utils.py:5623-5647
def _build_model_info(model, custom_llm_provider=None, api_base=None, api_key=None) -> ModelInfo:
    supported_openai_params = litellm.get_supported_openai_params(
        model=model, custom_llm_provider=custom_llm_provider
    )
    _model_info = _get_model_info_helper(model, custom_llm_provider, api_base, api_key)
    provider_info = get_provider_info(model=model, custom_llm_provider=custom_llm_provider)
    if provider_info:
        for key, value in provider_info.items():
            if value is not None:
                _model_info[key] = value
    return ModelInfo(**_model_info, supported_openai_params=supported_openai_params)
```

分三步：

1. 单独查 `supported_openai_params`（**这是 `_get_model_info_helper` 被拆出来的原因**：
   helper 的 docstring 明说「Separated out to avoid infinite loop caused by returning
   'supported_openai_param's」，`utils.py:5281-5282`）。
2. 调 helper 拿 `ModelInfoBase`。
3. 用 `get_provider_info` 的 provider 级信息**逐键覆盖**，且**只覆盖非 None 值**。

> 层次关系：`ModelInfoBase`（成本映射表事实）→ 叠加 provider 特定信息 →
> 加上 `supported_openai_params` → 组成 `ModelInfo`。

---

## 四、核心：`_get_model_info_helper` 的完整流程

`utils.py:5274-5620`。整个函数体包在一个 `try` 里，末尾统一转异常（见 4.6）。

### 4.1 前置名称规范化（`:5284-5295`）

```python
azure_llms = {**litellm.azure_llms, **litellm.azure_embedding_models}
if model in azure_llms:
    model = azure_llms[model]
if custom_llm_provider == "vertex_ai_beta":
    custom_llm_provider = "vertex_ai"
if custom_llm_provider == "vertex_ai":
    if "meta/" + model in litellm.vertex_llama3_models:
        model = "meta/" + model
    elif model + "@latest" in litellm.vertex_mistral_models or model + "@latest" in litellm.vertex_ai_ai21_models:
        model = model + "@latest"
```

即：Azure 别名映射、`vertex_ai_beta` 归一为 `vertex_ai`、
Vertex 上 llama3 补 `meta/` 前缀、Vertex 上 mistral/ai21 补 `@latest` 后缀。

### 4.2 构造 5 个候选名：`_get_potential_model_names`

`utils.py:5162-5197`。产出一个 `PotentialModelNamesAndCustomLLMProvider` TypedDict，
含 `split_model` / `combined_model_name` / `stripped_model_name` /
`combined_stripped_model_name` / `custom_llm_provider`。

三个分支：

| 输入形态 | 分支行为 |
| --- | --- |
| `custom_llm_provider is None` | 调 `get_llm_provider(model)` 反推 provider 与 `split_model`；**异常时兜底** `split_model = model`（`:5168-5169`）。`combined = model` |
| provider 已给且 `model` 以 `"{provider}/"` 开头 | `split_model = model.split("/", 1)[1]`；`combined = model`；`combined_stripped = f"{provider}/{stripped}"` |
| provider 已给但 model 无前缀 | `split_model = model`；`combined = f"{provider}/{model}"` |

额外的 bedrock 处理（`:5186-5189`）：provider 属 `bedrock` / `bedrock_converse` 时，
对 `split_model` 再过一次 `strip_bedrock_routing_prefix`，剥掉跨区路由前缀。

#### `_strip_model_name` 的剥离规则（`:4929-4945`）

按 `custom_llm_provider` 分派，**顺序敏感**：

1. `bedrock` / `bedrock_converse` → `_get_base_bedrock_model`
2. `vertex_ai` / `gemini` / `databricks` → `_strip_stable_vertex_version`（剥版本号后缀）
3. 模型名含 `"ft:"` → `_strip_openai_finetune_model_name`（把微调模型收敛到基座名）
4. 否则原样返回

> 注意第 3 条用的是 `"ft:" in model`（子串包含），不是 `startswith`。

### 4.3 旁路一：provider 动态 `get_model_info`（`:5308-5331`）

在查静态表**之前**，先尝试 provider 自己的动态实现：

```python
provider_config = ProviderConfigManager.get_provider_model_info(
    model=model, provider=LlmProviders(custom_llm_provider)
)  # 仅当 custom_llm_provider in LlmProvidersSet
if provider_config is not None:
    provider_get_model_info = getattr(provider_config, "get_model_info", None)
    if callable(provider_get_model_info):
        try:
            provider_model_info = provider_get_model_info(model=model, api_base=api_base, api_key=api_key)
            if provider_model_info is not None:
                return provider_model_info      # 直接返回，短路后续全部逻辑
        except Exception as e:
            verbose_logger.warning(
                "Could not get dynamic model info for model=%s, provider=%s; "
                "falling back to the static cost map: %s", model, custom_llm_provider, e
            )
```

**这是 `api_base` / `api_key` 唯一的实际用途**——它们只透传给 provider 的动态查询。
失败**不抛**，只 warning 后继续走静态表。

### 4.4 特例：huggingface（`:5333-5352`）

`custom_llm_provider == "huggingface"` 时完全不查成本映射表，而是
`_get_max_position_embeddings(model)` —— 去 `https://huggingface.co/{model}/raw/main/config.json`
拉 `max_position_embeddings`（`:5200-5220`，任何异常都 `return None`）。
返回的 `ModelInfoBase` 里 `input_cost_per_token=0`、`output_cost_per_token=0`、`mode="chat"`。

> 即：**HF 模型在 litellm 里没有价格，只有窗口，而且窗口靠实时抓 HF 页面**。

### 4.5 主路径：5 级候选名分层查找（`:5363-5415`）

源码里有一段明确的注释说明顺序（`:5354-5361`，"in order of specificity"）：

| 顺序 | 候选名 | 注释给的例子 |
| --- | --- | --- |
| 1 | `combined_model_name` | model=`llama3-8b-8192` + provider=`groq` → 查 `groq/llama3-8b-8192` |
| 2 | `model` | 查 `gemini-1.5-pro-002` |
| 3 | `split_model` | model=`bedrock/au.anthropic.claude-opus-4-8` → 查 `au.anthropic.claude-opus-4-8` |
| 4 | `combined_stripped_model_name` | 给 `gemini/gemini-1.5-flash-001` → 查 `gemini/gemini-1.5-flash` |
| 5 | `stripped_model_name` | 给 `ft:gpt-3.5-turbo:my-org:custom_suffix:id` → 查 `ft:gpt-3.5-turbo` |

每一级的代码形状完全一致（这是 5 段重复的 if 块，**litellm 自身的代码坏味**）：

```python
_matched_key = _get_model_cost_key(<候选名>)
if _matched_key is not None:
    key = _matched_key
    _model_info = _get_model_info_from_model_cost(key=key)
    if not _check_provider_match(_model_info, model_cost_custom_llm_provider):
        _model_info = None          # 命中了但 provider 不匹配 → 丢弃，继续下一级
```

**关键语义**：命中 key 不等于采用。必须再过 provider 一致性校验；
不一致就把 `_model_info` 置回 None 继续下探。

#### `_get_model_cost_key`：大小写不敏感查找（`:5026-5064`）

这个函数被源码用大写 WARNING 标注了性能红线
（`:5030-5031`：「Only O(1) lookup operations are acceptable... called frequently during router operations」），
甚至有专门的覆盖率测试 `check_get_model_cost_key_performance.py` 守着允许的 O(n) helper 白名单。

查找顺序：

1. 精确命中 `potential_key in litellm.model_cost` → 直接返回（O(1)）。
2. 用全局小写映射 `_model_cost_lowercase_map`（懒构建）做 O(1) 大小写不敏感匹配。
3. 拿到 `matched_key` 后**必须再校验 `matched_key in litellm.model_cost`**
   —— 处理 `model_cost.pop()` 造成的陈旧条目（`:5054-5056`）。
4. 若映射里有但真表里没有（陈旧）→ `_handle_stale_map_entry_rebuild` 重建映射后重试（罕见 O(n)）。
5. 全部失败 → `None`。

配套的失效机制（`:4948-4974`）：

- `_model_cost_lowercase_map`：模块级懒构建的 `{key.lower(): key}`。
- `_model_cost_mutation_generation`：**单调递增计数器**，每次 `model_cost` 变更 +1。
  源码注释解释了它的必要性——消费方 memoize 派生状态时若只看 `len`/`id`，
  「key 增删配对」或「原地替换 value」会让 len/id 都不变，从而漏失效（`:4951-4954`）。
- `_invalidate_model_cost_lowercase_map()`：置空映射 + 计数器 +1 +
  **清掉两层 LRU**（`_cached_get_model_info.cache_clear()` 与
  `_cached_get_model_info_helper.cache_clear()`，`:4973-4974`）。

#### `_check_provider_match`：provider 一致性与白名单（`:5071-5111`）

先说**通过条件**：`custom_llm_provider` 为空，或条目的 `litellm_provider`
为 None / 缺失（视为「无 provider 约束」的通配），或两者字面相等。

> 关于 None 视为通配，docstring 明确解释了原因：`register_model` 可能通过
> `get_model_info` 把 `None` 落进表里（无 provider 的自定义部署），
> 归一化这两种情况才能让自定义定价持续生效（`:5075-5079`）。

不相等时进入**白名单豁免**（否则返回 False）：

| 豁免规则 | 说明 |
| --- | --- |
| `vertex_ai` ↔ `vertex_ai*` 前缀 | 前缀族匹配 |
| `fireworks_ai` ↔ `fireworks_ai*` 前缀 | 前缀族匹配 |
| `bedrock*` ↔ `bedrock*` 前缀 | 双向前缀族 |
| `litellm_proxy` | 特例：它不是 provider 而是代理，一律放行 |
| `azure_ai` → `azure` / `openai` | 源码注释：宁可按 OpenAI 价格算，也比记 0 成本好 |
| `github` | 允许 `github/<model>` 复用既有 provider 元数据 |

### 4.6 旁路二：泛化规则兜底（`:5417-5424`）

5 级全部落空后，才调 `_get_model_info_from_generalization`（见第五节）。
命中则 `key, _model_info = generalization`。

### 4.7 未命中：抛异常

两处抛出，注意**不是返回 None**：

```python
# utils.py:5426-5429  —— 内层
if _model_info is None or key is None:
    raise ValueError("This model isn't mapped yet. Add it here - https://...")

# utils.py:5616-5620  —— 外层 except 统一改写
except Exception as e:
    verbose_logger.debug("Error getting model info: %s", e)
    raise Exception(
        f"This model isn't mapped yet. model={model}, custom_llm_provider={custom_llm_provider}. Add it here - https://..."
    )
```

**这是一个对调用方很重要的坑**：外层 `except Exception` 把**函数体内任何异常**
（包括 provider 动态查询之外的编程错误、类型错误）都统一改写成
「This model isn't mapped yet」的裸 `Exception`。
原始异常只落在 `verbose_logger.debug`，**不串 `raise ... from e`**，
所以调用方拿不到根因，也无法靠异常类型区分「模型未收录」与「内部故障」。

> 本项目 `core/llm` 若依赖 litellm 查窗口，**必须自行包一层**：
> 捕获裸 `Exception` 并转成本项目自己的错误码，不能靠 litellm 的异常类型判别。

### 4.8 组装 `ModelInfoBase`（`:5430-5615`）

- **成本字段兜底**：`input_cost_per_token` / `output_cost_per_token` 为 None 时
  **默认为 0**，并打 `verbose_logger.debug`（`:5430-5448`）。
  注释写着 "default value to 0, be noisy about this"，但实际只用了 `debug` 级别。
- **主体**是一条超长 `ModelInfoBase(...)` 构造（约 160 行），
  逐字段 `_model_info.get(<key>, None)`。字段覆盖三大类：
  - 窗口：`max_tokens` / `max_input_tokens` / `max_output_tokens`
  - 定价：分层价（`*_above_128k/200k/272k/512k_tokens`）、
    service tier 变体（`*_flex` / `*_priority`）、
    缓存价（`cache_creation_*` / `cache_read_*` / `prompt_cache_min_tokens`）、
    多模态价（audio / image / video / character / second / query / batches）、
    `tiered_pricing`、`regional_processing_uplift_multiplier_eu/us`
  - 能力开关：`supports_*` 一大批（`vision` / `function_calling` / `tool_choice` /
    `prompt_caching` / `reasoning` / `adaptive_thinking` / `web_search` /
    各档 `*_reasoning_effort` / `computer_use` / `pdf_input` ...）
  - 限流：`tpm` / `rpm`
- **`litellm_provider` 兜底**：`_model_info.get("litellm_provider", custom_llm_provider)`（`:5573`）
  —— 表里没写就用调用方传的 provider 填。
- **动态补齐分层价**（`:5612-5614`）：

  ```python
  _ABOVE_THRESHOLD_COST_KEY = re.compile(r"_above_\d+k?_tokens$")   # :5271
  for cost_key, cost_value in _model_info.items():
      if cost_key not in returned_model_info and _ABOVE_THRESHOLD_COST_KEY.search(cost_key) is not None:
          returned_model_info[cost_key] = cost_value
  ```

  即：凡是以 `_above_<数字>[k]_tokens` 结尾、且没被显式列举的成本键，
  **自动透传**。这样新增一档阈值价（比如 `_above_1000k_tokens`）时无需改代码。

---

## 五、泛化规则：未收录模型的正则兜底

规则引擎在 `litellm_core_utils/fallback_generalizations.py`，
数据来自成本映射表 JSON 的 `fallback_generalizations.rules`（第 2.3 节已 pop 出）。

### 5.1 两种规则

模块 docstring（`:1-47`）定义得很清楚，**靠 `model_info` 的结构区分种类**：

- **ROUTING 规则**：`model_info` **只有** `litellm_provider` 一个键。
  仅供 `get_llm_provider` 做裸 id 的 provider 推断，**永不贡献模型信息**。
- **CAPABILITY 规则**：`model_info` 含除 `litellm_provider` 之外的任意键
  （`mode` / `supports_*` / 窗口 / 定价）。仅供 `get_model_info` 兜底。

编译分类在 `_compile_rule`（`:106-144`），install 时一次完成：

| `model_info` 形态 | 产出 |
| --- | --- |
| 无 `litellm_provider` | 仅 `_CapabilityRule` |
| 只有 `litellm_provider`（len==1） | 仅 `_RoutingRule` |
| 有 `litellm_provider` **且**有其他键（legacy 混合） | **同时**产出 `_RoutingRule` + `_CapabilityRule` |
| `pattern` 非 str 或 `model_info` 非 dict | warning + 跳过 |
| 正则编译失败（`re.error`） | warning + 跳过 |
| `litellm_provider` 非 str | warning + 跳过 |

**legacy 兼容**：`_resolve_legacy_extends`（`:62-88`）在分类前把老 schema 的
`extends` 继承展开一层（父 `model_info` 打底，子键覆盖）。
docstring 说明这是临时 shim——已发布的 proxy 会从 main 远程拉到旧 schema 的块，
所以选择「容忍并当两种规则用」而不是崩溃（`:22-32`）。

### 5.2 匹配语义

```python
# fallback_generalizations.py:170-176
def match_capabilities(self, model: str) -> dict | None:
    if not model:
        return None
    matched = tuple(rule.model_info for rule in self.capability_rules if rule.pattern.search(model) is not None)
    if not matched:
        return None
    return {key: value for model_info in matched for key, value in model_info.items()}
```

- 用 `re.search` + `re.IGNORECASE`，**不隐式加锚点**——
  规则要绑定整名必须自己写 `^...$`，否则是子串匹配（docstring `:37-40`）。
- capability 规则取**所有命中规则的并集**，文件顺序在后的覆盖在前的。
- 对比之下 routing 规则是 `next(...)` **首个命中即返回**（`:162-168`）。
- 复杂度 O(规则数)，docstring 反复强调**只能在精确查找 miss 后调用**。

### 5.3 `_get_model_info_from_generalization` 的关键约束

`utils.py:5125-5159`。除了「按同样 5 个候选名同序尝试」，有一条**非常重要的前置守卫**：

```python
candidates = (combined_model_name, model, split_model, combined_stripped_model_name, stripped_model_name)
if any(_get_model_cost_key(candidate) is not None for candidate in candidates):
    return None          # 只要任一候选名是成本表里的精确 key，就放弃泛化
```

docstring 解释了理由（`:5135-5141`）：如果任一候选名是精确 key，
说明模型**是已知的**（只是 provider 不匹配才走到这一步），而不是「未收录」。
此时若用规则返回一个**无价格**的条目，会让那些还有「有价格的精确名」可试的调用方
（例如成本计算器的模型名变体 fallback 阶梯）拿到无价条目，反而更糟。

命中后 provider 回填：`custom_llm_provider` 非 None 时
用 `{**generalized_info, "litellm_provider": custom_llm_provider}` 覆盖（`:5156-5158`）。

---

## 六、缓存体系

### 6.1 两层 LRU

```python
# utils.py:5223-5238
@lru_cache(maxsize=DEFAULT_MAX_LRU_CACHE_SIZE)
def _cached_get_model_info_helper(model, custom_llm_provider, api_base=None) -> ModelInfoBase:
    """_get_model_info_helper wrapped with lru_cache — Speed Optimization to hit high RPS"""
    return _get_model_info_helper(model=model, custom_llm_provider=custom_llm_provider, api_base=api_base)

# utils.py:5650-5656
@lru_cache(maxsize=DEFAULT_MAX_LRU_CACHE_SIZE)
def _cached_get_model_info(model, custom_llm_provider=None, api_base=None) -> ModelInfo:
    return _build_model_info(model=model, custom_llm_provider=custom_llm_provider, api_base=api_base)
```

`DEFAULT_MAX_LRU_CACHE_SIZE` 定义在 `constants.py:342`：

```python
DEFAULT_MAX_LRU_CACHE_SIZE: Final = int(os.getenv("DEFAULT_MAX_LRU_CACHE_SIZE", 64))
```

即**默认只缓存 64 项**，可用同名环境变量调整。

> 值得注意：`get_model_info` 走的是 `_cached_get_model_info`（外层，返回 `ModelInfo`）。
> `_cached_get_model_info_helper`（内层，返回 `ModelInfoBase`）是给其他调用方用的，
> **`get_model_info` 这条链路上并不经过它** —— `_build_model_info` 直接调
> 未加缓存的 `_get_model_info_helper`（`utils.py:5631`）。

### 6.2 失效路径

`_invalidate_model_cost_lowercase_map()`（`:4962-4974`）是唯一的统一失效点，
它同时清掉上面两层 LRU。触发方：`register_model` 每写一个条目就调一次（`:2813`）。

外部还可以直接用挂在函数上的 `get_model_info.cache_clear()`（`:5742`）。

---

## 七、写入侧：`register_model` 如何影响 `get_model_info`

`utils.py:2715-2813`。这是**唯一**的官方写入通道，支持传 dict 或 URL
（传 str 时会 `litellm.get_model_cost_map(url=model_cost)` 去拉，`:2742`）。

几个和 `get_model_info` 强相关的细节：

- **`persist_across_reloads=True`（默认）**：把注册项存进 `_runtime_registered_model_cost`，
  成本映射表刷新时重放。docstring 说明默认为 True 是因为
  「注册模型的调用方在声明持久意图」；只描述单次请求时应传 False（`:2730-2734`）。
- **跳过 `get_model_info` 的 provider**：`github_copilot` / `chatgpt`
  （`_skip_get_model_info_providers`，`:2751-2754`）。
  原因写在注释里——这些 provider 调 `get_model_info` 会触发 OAuth 等副作用。
- **两处「防污染」清理**，都是为了不把 `get_model_info` 的**合成值**写回真表：
  1. `litellm_provider is None` → `pop` 掉（`:2791-2792`）。
     否则 `_check_provider_match` 会在后续查找中丢掉自定义定价。
     （与 4.5 节 `_check_provider_match` 把 None 当通配的处理互为呼应。）
  2. `input_cost_per_token` / `output_cost_per_token`：
     若原始条目和本次传值里**都没有**这个字段，就 `pop` 掉（`:2793-2807`）。
     注释（引用 issue #30198）解释：`_get_model_info_helper` 会把缺失的成本
     合成为 0（见 4.8），写回去会让条目从「无成本键（按名定价）」
     变成「成本键=0（免费）」，导致 `_is_cost_explicitly_configured` 返回 True，
     **静默关掉预算强制**。
- 最后 `_update_dictionary(existing_model, value)` 合并，
  `litellm.model_cost.setdefault(model_cost_key, {}).update(...)` 落盘，
  再 `_invalidate_model_cost_lowercase_map()`（`:2808-2813`）。

---

## 八、完整流程图

```
get_model_info(model, custom_llm_provider, api_base, api_key)        utils.py:5659
│
├─ api_key is not None ? ──── YES ──> _build_model_info(...)  【绕过 LRU】
│                                        │
└─ NO ──> _cached_get_model_info(...)  【LRU, key=(model,provider,api_base), maxsize=64】
                │
                └──> _build_model_info                                utils.py:5623
                        │
                        ├─ 1. litellm.get_supported_openai_params(...)
                        │
                        ├─ 2. _get_model_info_helper(...)             utils.py:5274
                        │      │
                        │      ├─ (a) 名称规范化: azure 别名 / vertex_ai_beta→vertex_ai
                        │      │        / meta/ 前缀 / @latest 后缀        :5284
                        │      │
                        │      ├─ (b) _get_potential_model_names        :5162
                        │      │        → combined / model / split
                        │      │          / combined_stripped / stripped
                        │      │        （bedrock 再剥路由前缀）
                        │      │
                        │      ├─ (c) 旁路①: provider_config.get_model_info(
                        │      │            model, api_base, api_key)     :5313
                        │      │        命中 → return（短路全部后续）
                        │      │        异常 → warning，继续
                        │      │
                        │      ├─ (d) 特例: huggingface → 抓 HF config.json
                        │      │            取 max_position_embeddings，价格=0  :5333
                        │      │
                        │      ├─ (e) 主路径: 5 级候选名查 litellm.model_cost   :5367
                        │      │        每级 = _get_model_cost_key（精确→小写映射→陈旧重建）
                        │      │               + _check_provider_match（不匹配则丢弃下探）
                        │      │
                        │      ├─ (f) 旁路②: _get_model_info_from_generalization :5417
                        │      │        守卫: 任一候选名是精确 key → 放弃泛化
                        │      │        否则按同序候选名跑 capability 正则规则并集
                        │      │
                        │      ├─ (g) 仍为 None → raise ValueError
                        │      │        → 被外层 except 改写为裸 Exception   :5616
                        │      │
                        │      └─ (h) 组装 ModelInfoBase                    :5450
                        │               成本缺失兜底 0 / provider 兜底调用方值
                        │               / _above_\d+k?_tokens 分层价自动透传
                        │
                        ├─ 3. get_provider_info(...) 逐键覆盖（仅非 None）    :5638
                        │
                        └─ 4. ModelInfo(**_model_info, supported_openai_params=...)
```

---

## 九、对本项目的可行动结论

按重要性排序，均基于上文源码事实：

1. **必须包一层异常转换**。`_get_model_info_helper` 的外层
   `except Exception` 把所有异常改写成裸 `Exception`，且不带 `from e`（4.7 节）。
   `core/llm` 里任何 `get_model_info` 调用都要捕获裸 `Exception`
   并转成本项目的错误码，不能试图按异常类型分辨「未收录」与「内部故障」。

2. **确认是否要接受启动期网络请求**。`import litellm` 默认拉 GitHub JSON（2.1 节）。
   若要求可复现 / 离线可用，应设 `LITELLM_LOCAL_MODEL_COST_MAP=true`
   走本地备份（2.2 节路径 1）。这直接关系桌面端首启耗时与断网可用性。

3. **窗口回填要认 `max_input_tokens` 而非 `max_tokens`**。
   两者在 `ModelInfoBase` 里是独立字段（4.8 节），
   `max_tokens` 语义偏「总量/输出上限」且各 provider 填法不一致。
   本项目 `context_window_resolver` 取输入侧窗口时应优先 `max_input_tokens`。

4. **价格 0 不代表免费**。成本字段缺失时被兜底成 0 且只打 `debug` 日志（4.8 节）。
   若本项目要做成本估算，需区分「显式 0」与「缺失兜底 0」，
   否则会静默把未知价格算成免费——这与 litellm 自己在
   `register_model` 里防的 #30198 是同一类坑（第七节）。

5. **DeepSeek 走 `deepseek/` 前缀可命中第 1 级候选名**。
   本项目经 `ChatLiteLLM` 接 DeepSeek，模型名带 `deepseek/` 前缀时，
   `combined_model_name` 即 `deepseek/<model>`，是特异性最高的一级（4.5 节）。
   自定义 / 新发布模型未收录时会落到泛化规则（第五节），
   拿到的条目**可能无价格**，需按第 4 条处理。

6. **LRU 只有 64 项**（6.1 节）。模型数量多时可用
   `DEFAULT_MAX_LRU_CACHE_SIZE` 环境变量上调；
   本项目模型数远小于 64，无需调整。

---

## 十、本文引用的源码位置索引

| 符号 | 文件 | 行 |
| --- | --- | --- |
| `model_cost_map_url` | `litellm/__init__.py` | 405-408 |
| `model_cost = get_model_cost_map(...)` | `litellm/__init__.py` | 523 |
| `get_model_cost_map` | `litellm_core_utils/get_model_cost_map.py` | 426-478 |
| `_finalize_model_cost_map` | `litellm_core_utils/get_model_cost_map.py` | 414-423 |
| `_expand_model_aliases` | `litellm_core_utils/get_model_cost_map.py` | 359-411 |
| `get_model_cost_map_source_info` | `litellm_core_utils/get_model_cost_map.py` | 336-351 |
| 规则引擎 docstring | `litellm_core_utils/fallback_generalizations.py` | 1-47 |
| `_resolve_legacy_extends` | `litellm_core_utils/fallback_generalizations.py` | 62-88 |
| `_compile_rule` | `litellm_core_utils/fallback_generalizations.py` | 106-144 |
| `match_capability_generalizations` | `litellm_core_utils/fallback_generalizations.py` | 206-212 |
| `DEFAULT_MAX_LRU_CACHE_SIZE` | `litellm/constants.py` | 342 |
| `register_model` | `litellm/utils.py` | 2715-2813 |
| `_strip_stable_vertex_version` | `litellm/utils.py` | 4897 |
| `_get_base_bedrock_model` | `litellm/utils.py` | 4901 |
| `_strip_openai_finetune_model_name` | `litellm/utils.py` | 4913 |
| `_strip_model_name` | `litellm/utils.py` | 4929-4945 |
| `_invalidate_model_cost_lowercase_map` | `litellm/utils.py` | 4962-4974 |
| `_get_model_cost_key` | `litellm/utils.py` | 5026-5064 |
| `_check_provider_match` | `litellm/utils.py` | 5071-5111 |
| `_get_model_info_from_generalization` | `litellm/utils.py` | 5125-5159 |
| `_get_potential_model_names` | `litellm/utils.py` | 5162-5197 |
| `_get_max_position_embeddings` | `litellm/utils.py` | 5200-5220 |
| `_cached_get_model_info_helper` | `litellm/utils.py` | 5223-5238 |
| `get_provider_info` | `litellm/utils.py` | 5241-5256 |
| `_ABOVE_THRESHOLD_COST_KEY` | `litellm/utils.py` | 5271 |
| `_get_model_info_helper` | `litellm/utils.py` | 5274-5620 |
| `_build_model_info` | `litellm/utils.py` | 5623-5647 |
| `_cached_get_model_info` | `litellm/utils.py` | 5650-5656 |
| `get_model_info` | `litellm/utils.py` | 5659-5739 |
| `get_model_info.cache_clear/cache_info` | `litellm/utils.py` | 5742-5743 |
