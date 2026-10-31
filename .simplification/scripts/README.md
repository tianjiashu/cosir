# 检测器索引

本项目全部精简检测器的唯一登记处。`run-all.mjs` 按下表的「文件」列依次运行，没有登记的脚本不会被运行。

```bash
node .simplification/scripts/run-all.mjs --scope <scope.json> [--out <候选.json>]
```

- `scope.json`：`files` 为相对仓库根的文件清单（code-simplification skill 的 `scope.mjs --json` 输出，或人工指定范围时手写）。不传 `--scope` 时全量扫描。
- 每个检测器把候选逐行输出为 JSON：`{ detector, transform, tier, file, line, symbol, evidence }`。`run-all.mjs` 合并后写到 `--out`（默认在系统临时目录），并打印按检测器与条款的计数。
- 检测器只读，不修改代码。档位与条款编号含义见 code-simplification skill 随附的精简标准 §4、§5.1。
- 脚本只是辅助：没有报告不等于没有问题，范围内的代码仍要人工阅读判断。

| 名称 | 文件 | 档位 | 条款 | 包装的项目工具 | 已知局限 | 最近修改原因 |
|---|---|---|---|---|---|---|
| knip | `knip.mjs` | 二 | D2、D1 | `apps/desktop` 的 knip（`knip.jsonc`） | 看不到注册表、按字符串分发与跨进程引用；不报 unlisted 与 duplicates | 2026-10-31 首轮建立 |
| tsc-unused | `tsc-unused.mjs` | 一（局部）/ 二（参数） | D1、D3 | TypeScript 编译器，检测时临时开启 `noUnusedLocals` / `noUnusedParameters` | `_` 开头的参数不报；只为副作用存在的导入不报 | 2026-10-31 首轮建立 |
| eslint-defense | `eslint-defense.mjs` | 二 | F1、F3、F4 | ESLint + typescript-eslint（`no-unnecessary-condition`、`no-useless-catch`、`no-empty`） | 类型写错时 F1 结论不可靠（如 `JSON.stringify` 的返回值类型）；异步回调里改写的局部标志会被误判恒真；跨进程输入的类型只是约定，不能只凭类型判多余；不查 tests、scripts | 2026-10-31 首轮：检测时开启 `noUncheckedIndexedAccess`（下标访问误报 127→32）、允许常量循环条件 |
| catch-swallow | `catch-swallow.mjs` | 二 | F3、F4 | TypeScript 语法树（无新依赖） | 只看 catch 块本身；有意的失败安全也会被报出 | 2026-10-31 首轮建立 |
| dir-names | `dir-names.mjs` | 一（S8）/ 二（C4） | S8、C4 | 无（纯路径检查） | 只查范围内文件所在目录；名单是启发式 | 2026-10-31 首轮建立 |

`lib.mjs` 是检测器共用的工具模块，不是检测器，不登记。

## 尚未覆盖

- **重复代码（E5、M1、M3）**：项目没有重复检测工具。可选 jscpd，需要人类同意引入新依赖后再接入。
- **复杂度（E7）**：项目没有复杂度统计工具。可选 lizard 或 ESLint `complexity` 规则；后者不需要新依赖，可在下一轮接入。
