# Python 代码开发规范

> 本规范是 `rules/Agent代码开发规范.md`（技术栈无关通用规范）的 **Python 专属补充**。
> 通用规范已覆盖的原则（单一职责、不重复造轮子、防膨胀、docstring、分层架构、可排查日志、开发-审查-测试闭环）**此处不再重复**，只补充 Python 生态特有的约定与工具链。阅读本规范前请先读通用规范。

---

## 一、定位与范围

- **适用对象**：`apps/backend/` 下的 Python 后端代码（FastAPI + LangGraph + SQLAlchemy）。
- **不重复原则**：通用规范的原则层（正确性 > 单一职责 > …）在本语言下同样强制，本规范只补工具链与 Python 约定。
- **强制方式**：规范通过 `pyproject.toml` 中的 Ruff / mypy / pytest 配置 + pre-commit 钩子**强制**，而非仅靠人工遵守。

---

## 二、工具链与自动化

工具链以 **Ruff 一体** 承担格式化、lint 与 import 排序；**mypy 渐进式** 做类型检查；**uv** 管理依赖与锁；**pre-commit** 在提交前强制。

### 2.1 统一配置源

所有 Python 工具配置集中在 `apps/backend/pyproject.toml` 的 `[tool.ruff]` / `[tool.mypy]` / `[tool.pytest.ini_options]` 段，**不**再使用独立的 `.flake8` / `setup.cfg` / `tox.ini` / `ruff.toml`。

### 2.2 Ruff（format + lint + import 排序）

- 行宽 **100**（后端 FastAPI 长类型注解多，比 black 默认 88 更贴合）。
- 字符串统一 **双引号**（`quote-style = "double"`），与现有代码一致。
- 目标版本 `py310`。
- 启用的规则集（实用组合，不追求最严）：

  | 规则 | 含义 |
  |------|------|
  | `E` `W` `F` | pycodestyle 错误/警告 + pyflakes（未定义/未使用） |
  | `I` | isort import 排序 |
  | `UP` | pyupgrade 现代化语法（PEP 585/604） |
  | `B` | flake8-bugbear 防常见 bug |
  | `C4` | flake8-comprehensions |
  | `SIM` | flake8-simplify |
  | `ASYNC` | flake8-async 异步陷阱 |
  | `PYI` | 类型存根一致性 |
  | `RUF` | Ruff 专属规则 |
  | `S` | flake8-bandit 安全（后端涉及 API Key，有意义） |

- **忽略项**：
  - `S101`：测试/脚本中 `assert` 允许。
  - `RUF001` / `RUF002` / `RUF003`：中文文档串/注释/字符串中的“歧义 unicode”多为中文标点误报，项目中文优先，忽略以免噪音淹没真实问题。
- **按文件忽略**：
  - `tests/**`：`S101` `S603` `S607`（测试允许 assert 与子进程调用类规则）。
  - `app/api/**`：`B008`（FastAPI 用 `Depends()` 作默认参数是惯用法，属误报）。

### 2.3 mypy（渐进式类型检查）

- `python_version = "3.10"`，`ignore_missing_imports = true`（第三方无 stub 时不阻塞）。
- `disallow_untyped_defs = false`：**存量**代码未全注解，不卡存量；**新代码必须带类型注解且通过 mypy**。
- `warn_unused_ignores` / `warn_redundant_casts` / `warn_unreachable` = true，逐步收紧。
- pre-commit 中 mypy 当前设为 **非阻塞**（`continue-on-error: true`），仅提示不报错；待存量注解补全后移除该设置升级为强制。

### 2.4 pre-commit

- 配置：`apps/backend/.pre-commit-config.yaml`，挂 `ruff-format` + `ruff --fix` + `mypy`。
- 安装：`uv run pre-commit install`（在 `apps/backend` 下）。
- 提交前自动格式化、lint、类型提示，避免“规范靠人记”。

### 2.5 uv 依赖管理（单一来源）

- 运行时依赖写在 `[project.dependencies]`，开发依赖写在 `[dependency-groups].dev`，**不**再用 `requirements.txt`（已删除）。
- 锁文件 `uv.lock` 必须提交，保证可复现构建。
- 开发环境：`uv sync --group dev`。
- 常用命令（均在 `apps/backend` 下执行）：
  - `uv run ruff check app tests` — lint
  - `uv run ruff format app tests` — 格式化
  - `uv run mypy app` — 类型检查
  - `uv run pytest` — 测试

---

## 三、风格约定

- 基准 **PEP 8**，以 Ruff 默认 + 上述微调为准。
- **命名**：
  - 函数 / 变量：`snake_case`
  - 类：`PascalCase`
  - 常量：`UPPER_SNAKE_CASE`
  - 模块 / 包：`lower_snake_case`
  - 私有成员：单下划线前缀 `_name`（不鼓励双下划线，避免命名改写）。
  - 禁止 `Utils` / `Helper` / `Common` / `Misc` / `Manager` 等模糊命名（见通用规范）。
- **导入**：
  - 三级分组：标准库 → 第三方 → 本地（`app.`），由 Ruff `I` 自动排序。
  - **绝对导入** `from app.xxx import yyy`，不使用相对导入跨包。
- 文件编码 **UTF-8**；每个模块顶部有模块级 docstring 说明职责。
- 字符串用双引号；f-string 必须有占位符（避免 `F541`）。

---

## 四、类型注解与运行时约定

- **公共接口（模块对外 API、业务/工具入口、文件/网络/DB 操作、异步函数）必须带完整类型注解**；新代码注解缺失视为不达标。
- 允许在模块顶部 `from __future__ import annotations`，以便 3.10 使用 `X | None`、`list[int]` 等现代写法。
- **异步一致性**：IO 密集路径用 `async/await`，不混用 sync/async 危险模式；FastAPI 路由与 LangGraph node 保持异步一致。
- **异常**：
  - 建立项目自定义异常层级，抛出具体异常，**不抛裸 `Exception`**。
  - 禁止空 `except:` / `except Exception: pass`（通用规范铁律）；确需吞掉时用 `contextlib.suppress(...)` 并留日志。
- 数据校验优先用 **Pydantic v2**（`pydantic.BaseModel`），不在业务层手写校验。

---

## 五、模块与包结构

- **绝对导入** `app.` 作为包引用唯一形式（见第三、四章）。
- **`__all__` 显式导出**：包/模块对外符号用 `__all__` 列出（如 `react_like.py` 的薄 re-export 壳），便于静态分析与工具识别公共 API。
- **`__init__.py` 只做薄壳 re-export**，不放业务逻辑；逻辑下沉到子模块。
- **禁止循环导入**：出现循环说明职责边界或分层有问题，应下沉到更底层模块或抽接口（见通用规范分层架构）。
- `import *`（`from x import *`）**禁止**：会导致 `F403`/`F405`，破坏静态分析与 mypy，且隐藏真实依赖。当前存量 114 处，按渐进清理，新代码一律显式导入。

---

## 六、依赖与可复现

- **uv 单一来源**：依赖只写在 `pyproject.toml`，由 `uv.lock` 锁定版本；不手写未声明依赖。
- 运行时依赖与开发依赖分组（`[project.dependencies]` vs `[dependency-groups].dev`），开发工具不进入运行时。
- 新增依赖必须**锁版本**（`==x.y.z`），评估必要性后再引入（通用规范：不重复造轮子 + 锁版本）。
- 最低运行环境 **Python 3.10+**（与 LangGraph 门控一致），开发目标版本见 `.python-version`。

---

## 七、测试约定

- 框架 **pytest**；测试目录 `apps/backend/tests/`。
- 文件 `test_*.py`，函数 `test_*`（或类 `Test*`），命名清晰表达验证点。
- `pytest-asyncio` 配 `asyncio_mode = "auto"`，异步测试无需手动标记。
- 运行：`uv run pytest`（或 `uv run pytest tests/test_xxx.py` 单文件）。
- **覆盖率**：当前渐进，先不卡死门槛；新功能应补对应测试，关键路径（工具执行、checkpoint、审批、日志）必须有测试。
- 测试须可独立运行、无外部副作用；涉及外部依赖失败时验证日志可排查（见通用规范日志章与测试闭环）。

---

## 八、与通用规范的关系

| 通用规范条款 | 本规范补充 |
|------|------|
| 单一职责 | 模块/包结构章：`__init__` 薄壳、禁止循环导入、绝对导入 |
| 不重复造轮子 | 依赖章：uv 单一来源、锁版本、Pydantic 校验 |
| 防膨胀 | 工具链章：Ruff 自动统一格式，避免“先堆后整理”的格式债 |
| 函数 Docstring | 类型注解章：注解与 docstring 互补，新代码强制注解 |
| 分层架构 | 模块结构章：`import *` 禁止、显式 `__all__` 导出 |
| 可排查日志 | 异常章：禁止空 except，用 `contextlib.suppress` + 日志 |
| 开发-审查-测试闭环 | 测试章：pytest 约定；工具链章：pre-commit 强制门禁 |

---

## 九、存量治理说明（渐进）

落地本规范时首次 `ruff check` 存量约 169 处，主要为：

- **`F405`/`F403`（`import *`）114 处**：真实代码质量债，禁止新代码使用，存量按模块逐步改为显式导入。
- **`S108`/`S105`（硬编码临时文件/密码字符串）**：安全相关，需逐一 review 确认无泄露风险。
- **`E501`/`E402`/`RUF012`/`UP035`/`B039` 等**：格式与现代化，随改动逐步收敛。

**原则**：新代码必须 100% 符合本规范（Ruff 通过、新代码注解、无 `import *`）；存量不要求一次性清零，但每次 touching 文件时顺手收敛其 lint 问题（Boy Scout Rule）。
