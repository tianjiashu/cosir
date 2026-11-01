# 检测器与指标采集器索引

本项目全部精简检测器与指标采集器的唯一登记处。`run-all.mjs` 按下表的「文件」与「类别」列运行，没有登记的脚本不会被运行。

```bash
node .simplification/scripts/run-all.mjs [--scope <scope.json>] [--metrics] [--json]
```

- `scope.json`：`files` 为相对仓库根的文件清单（code-simplification skill 的 `scope.mjs --json` 输出，或人工指定范围时手写）。不传 `--scope` 时全量扫描。
- 默认运行检测器，`--metrics` 只运行采集器；`--json` 把合并结果以 JSON 数组写到 stdout（进度走 stderr），不带时打印可读摘要。
- 检测器把候选逐行输出为 JSON：`{ detector, transform, tier, path, line, symbol, evidence }`；采集器逐行输出 `{ metric, value, details }`。
- 检测器只读，不修改代码。档位与条款编号含义见 code-simplification skill 随附的精简标准 §4、§5.1。
- 脚本只是辅助：没有报告不等于没有问题，范围内的代码仍要人工阅读判断。

| 名称 | 文件 | 类别 | 档位 | 条款 | 包装的项目工具 | 已知局限 | 最近修改原因 |
|---|---|---|---|---|---|---|---|
| knip | `knip.mjs` | 检测器 | 二 | D2、D1 | `apps/desktop` 的 knip（`knip.jsonc`） | 看不到注册表、按字符串分发与跨进程引用；不报 unlisted 与 duplicates | 2026-10-31 首轮建立 |
| tsc-unused | `tsc-unused.mjs` | 检测器 | 一（局部）/ 二（参数） | D1、D3 | TypeScript 编译器，检测时临时开启 `noUnusedLocals` / `noUnusedParameters` | `_` 开头的参数不报；只为副作用存在的导入不报 | 2026-10-31 首轮建立 |
| eslint-defense | `eslint-defense.mjs` | 检测器 | 二 | F1、F3、F4 | ESLint + typescript-eslint（`no-unnecessary-condition`、`no-useless-catch`、`no-empty`） | 类型写错时 F1 结论不可靠（如 `JSON.stringify` 的返回值类型）；异步回调里改写的局部标志会被误判恒真；跨进程输入的类型只是约定，不能只凭类型判多余；不查 tests、scripts | 2026-10-31 首轮：检测时开启 `noUncheckedIndexedAccess`（下标访问误报 127→32）、允许常量循环条件 |
| catch-swallow | `catch-swallow.mjs` | 检测器 | 二 | F3、F4 | TypeScript 语法树（无新依赖） | 只看 catch 块本身；有意的失败安全也会被报出 | 2026-10-31 首轮建立 |
| dir-names | `dir-names.mjs` | 检测器 | 一（S8）/ 二（C4） | S8、C4 | 无（纯路径检查） | 只查范围内文件所在目录；名单是启发式 | 2026-10-31 首轮建立 |

`lib.mjs` 是共用的工具模块，不是检测器，不登记。

## 尚未覆盖

配方见 code-simplification skill 的 `references/detection-scripts.md`「指标三层」。

- **采集器一个都没有**：E5、E6 不可用，E7 由 skill 退回缩进近似。E7 可包装 ESLint 内置的 `complexity`、`max-depth` 规则（不需要新依赖）；E6 可用 TypeScript 编译器读 import 边；E5 项目没有重复检测工具，可选 jscpd，需要人类同意引入新依赖。
- **检测器只覆盖 `apps/desktop`**：后端 Python 没有检测器，`metrics.mjs` 的条款计数对后端恒为 0，不是没有问题。
- **M5、M6 检测器未写**：E4 与透传层计数为 0 只说明没有检测器。
- 2026-11-01：候选字段由 `file` 改为 `path`、总入口改为 `--json` 输出，对齐 skill 的统一输出契约（此前 `seeds.mjs` 读不到本项目候选的路径）。
