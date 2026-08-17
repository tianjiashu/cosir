# mypy 强类型存量迁移计划

> 状态：计划已定，存量迁移分批推进中。本文档与 `apps/backend/pyproject.toml` 的
> `[[tool.mypy.overrides]]`、`rules/Python代码开发规范.md`「强类型开发基线」互为对照。
> 修改豁免清单时必须同步更新本文档与规范文档。

## 一、背景与目标

- **目标（第零铁律）**：Python 代码向强类型开发演进——类型错误在写代码/提交前被抓住，而非运行时炸给用户。
- **基线**：`apps/backend/pyproject.toml` 中 `[tool.mypy] strict = true` 已就位（官方愿景），存量经 `[[tool.mypy.overrides]] module = ["app.*"]` 临时豁免。
- **新代码门禁（已生效）**：pre-commit `mypy-new-strict` hook 对本次提交**新增**的 `apps/backend/app/` 下 `.py` 文件强制 `mypy --strict`（`apps/backend/mypy.strict.ini`，无豁免），未通过阻断提交。
- **本次任务的终点**：逐目录去除存量豁免，直到 `[[tool.mypy.overrides]]` 整段删除、全量 `uv run --project apps/backend mypy apps/backend/app` 干净通过。

## 二、当前状态（2026-08-17）

| 项 | 状态 |
|----|------|
| `[tool.mypy] strict = true` | 已落地 |
| `[[tool.mypy.overrides]] module = ["app.*"]` 存量豁免 | 生效中（待分批收紧） |
| pre-commit `mypy-new-strict` 新增文件门禁 | 已落地 |
| 存量 `mypy` 全量 | 过渡期（豁免下应无阻塞错误，见验收记录） |
| litellm 精确锁 | `==1.97.0`（当前实际运行版本；1.83.14 与 `python-dotenv==1.0.1` 冲突不可用） |

## 三、分批清理计划

按依赖方向自底向上清理（叶子层先行，减少中间层依赖干扰）：

1. **批 1 — `app/models/`**（leaf 值对象，依赖最少，风险最低）
   - 做法：将 `app.models` 从 overrides 豁免中摘出（新增 `[[tool.mypy.overrides]] module = ["app.models.*"]` 设置 `disallow_untyped_defs = true` 等，或删除其对豁免的覆盖后收紧）。
   - 验收：`uv run --project apps/backend mypy apps/backend/app/models` 干净通过。
2. **批 2 — `app/tools/`**（工具系统，含 guard/web/security 子层）
   - 验收：全量 mypy 通过，且 pre-commit 门禁对新增工具文件仍生效。
3. **批 3 — `app/service/`**（领域服务编排）
   - 验收：全量 mypy 通过。
4. **批 4 — `app/core/`**（LangGraph 运行底座，第三方类型最多，最难，放最后）
   - 验收：全量 mypy 通过。
5. **批 5 — `app/api/` / `app/config/` / `app/storage/` / `app/utils/`（含 `trace_infra/`）/ `app/bootstate.py`**
   - 验收：全量 mypy 通过，删除整个 `[[tool.mypy.overrides]]` 段。

每批完成后执行：
- `uv run --project apps/backend mypy apps/backend/app`（全量必须干净）；
- `uv run --project apps/backend pytest`（回归全量测试）；
- 启动独立审查 Agent + 独立测试 Agent 闭环验收（规范第八条）。

## 四、操作守则

- **严禁**把新代码/新文件塞进豁免段——新文件由 pre-commit 门禁强制 strict，天然不受豁免约束（门禁用独立 `mypy.strict.ini`）。
- 每批改动**聚焦**：只摘豁免 + 补该目录内缺失注解，不顺手重构无关代码。
- 无法立即修好的类型问题（如第三方 stub 缺失）用 `# type: ignore[xxx]` + 注释说明原因，并记录到本文档「遗留项」。
- 豁免清单是**待清理债务**，每次合并涉及豁免变更的 PR 必须同步更新本文档与本规范。

## 五、遗留项记录

- （无）

## 六、相关文件

- `apps/backend/pyproject.toml` — `[tool.mypy]` / `[[tool.mypy.overrides]]`
- `apps/backend/mypy.strict.ini` — 新增文件强类型门禁配置（无豁免）
- `apps/backend/scripts/mypy_new_strict.sh` — 门禁脚本（过滤本次新增 app/ 下 .py）
- `apps/backend/scripts/mypy_nonblocking.sh` — 存量/修改文件非阻塞提示
- `.pre-commit-config.yaml` — `mypy-new-strict` hook
- `rules/Python代码开发规范.md` — 「强类型开发基线」约定
