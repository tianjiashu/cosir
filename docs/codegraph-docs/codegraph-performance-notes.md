# CodeGraph 实测性能记录

> 本文件记录 CodeGraph 在真实项目上的实测性能数据（耗时、输出、结论），
> 供设计决策（超时配置、同步/常驻策略、阻塞语义）参考。
> 每次实测追加一条记录，按日期倒序或正序排列，标注环境与仓库规模。

## 2026-08-04 · `codegraph init` 全量重建实测

- **环境**：Windows，仓库根 `H:\coding-agent`（已有 `.codegraph/`，本次为重新初始化 + 全量重建索引）
- **命令**：`codegraph init H:\coding-agent`
- **耗时**（PowerShell `Stopwatch`）：

| 阶段 | 耗时 |
|------|------|
| 整个 `init` 进程（含进程/Node 冷启动 + 索引） | **11.66s** |
| 纯索引动作（Scanning / Parsing / Resolving / Linking） | **1.8s** |
| 进程启动 + Node 冷启动 + CLI 加载 + 目录初始化 | **约 10s** |

- **输出**：

```
T  Initializing CodeGraph
|
*  Initialized in H:\coding-agent
|
Scanning files...
Parsing code...
Resolving refs...
Linking dynamic dispatch...
|
*  Indexed 679 files
|
  8,759 nodes, 30,912 edges in 1.8s
|
  Done
```

- **结论**：
  1. 索引算法本身很快：679 文件 / 8759 节点 / 30912 边，仅 1.8s。
  2. 主要瓶颈在**进程冷启动**（约 10s），非索引计算。
  3. 对比增量 `codegraph sync`（37 文件 1.1s 秒级完成），`init` 全量重建慢一个量级，差距主要来自启动开销 + 全量扫描。

## 对设计的启示

- 「创建 workspace 时同步建索引」若每次临时拉起 `codegraph` 进程，**首次 init 光冷启动就可能 10s+**；这印证了「常驻 Kernel」的价值——进程常驻只建一次，冷启动开销被吸收，索引本身 1.8s 是可接受范围。
- 方案 B（`POST /workspaces` 同步 ensure_ready）依赖常驻 Kernel 的 `ensure_ready`（经 RPC 调 `index_init/index_sync`），**不经过 CLI 冷启动**，因此实际耗时接近纯索引时间（1.8s 量级）而非 CLI 进程的 11.66s。
- 超时配置参考：`Settings.CODEGRAPH_INDEX_INIT_TIMEOUT_SECONDS=600`、`CODEGRAPH_INDEX_SYNC_TIMEOUT_SECONDS=120`（`apps/backend/app/config/settings.py`），对常驻 Kernel 下 1.8s 的实测有充足余量。
