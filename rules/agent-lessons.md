## 2026-07-06 11:14 CST - buji-main Netlify 部署包管理器误用

- 问题：在只有 Codex bundled `pnpm`、没有 `npm/npx` 的 shell 中，为了跑 Astro 构建直接执行了 `pnpm exec astro check && pnpm exec astro build`。
- 原因：项目原本由 npm 管理并存在 `package-lock.json`；`pnpm exec` 触发依赖状态检查，把 npm 安装的依赖移动到 `node_modules/.ignored`，并生成 `pnpm-lock.yaml` / `pnpm-workspace.yaml`，随后因 `esbuild` build script 未审批而失败。
- 修正方式：记录该副作用，清理由本次误用产生的 pnpm 文件，优先恢复或继续使用项目既有 npm 依赖布局；若环境缺少 npm/npx，应先寻找项目本地二进制、系统 Node/npm 或 Netlify 插件能力，而不是用 pnpm 接管依赖。
- 以后避免原则：部署前先确认项目包管理器锁文件；没有对应包管理器时，先补齐同类工具链或使用已有 `node_modules/.bin`，不要跨包管理器执行会修改依赖树的命令。

## 2026-07-06 12:24 CST - buji-main Vercel 部署需固定输出目录和站点 URL

- 问题：Vercel CLI 首次部署 Astro 项目时提示未检测到框架，默认输出目录规则不够明确；部署后页面可访问，但 canonical/OG/RSS 仍使用本地占位 `https://buji.example.com`。
- 原因：项目没有 `vercel.json` 固定 `buildCommand` / `outputDirectory`；生产环境也未设置 `SITE_URL`，Astro 构建只能使用代码里的占位值。
- 修正方式：删除 Netlify 配置，新增 `vercel.json` 指定 `npm run build` 和 `dist`，在 Vercel Production 环境添加 `SITE_URL=https://buji-main.vercel.app`，随后重新生产部署并验证首页 canonical 与 RSS。
- 以后避免原则：Astro 静态站切换部署平台时，要同时处理平台配置、环境变量和线上 SEO/RSS 输出验证；不能只看首页能打开就算部署完成。

## 2026-07-06 12:35 CST - Codex shell 中 GitHub CLI 与 Homebrew PATH 不一致

- 问题：初始化 `buji-main` Git 仓库后，普通 `git push` 因 HTTPS 凭据缺失失败；Codex shell 里 `gh` 和 `brew` 均不可见，GitHub 插件 contents API 对目标仓库写入又返回 `Resource not accessible by integration`。
- 原因：Codex 桌面 shell 的 PATH 与用户普通终端不完全一致，即使本机有 Homebrew，也可能不在当前执行环境可见；GitHub 插件授权和本机 Git 凭据是两套机制，能读仓库不代表能写 contents。
- 修正方式：临时下载 GitHub CLI 官方 macOS arm64 二进制到 `/tmp`，用 `gh auth login --web` 完成设备码授权，再执行 `gh auth setup-git` 让 git 使用 gh credential helper，最后 `git push -u origin main` 成功。
- 以后避免原则：推送 GitHub 前先检查 `gh auth status`、`git credential` 与实际 `git push` 是否同链路；Codex 环境找不到 Homebrew 时，不要反复假设 PATH 正常，直接定位绝对路径或使用临时官方 CLI。

## 2026-07-06 12:40 CST - Shell PATH 配置与 Codex Node 原生模块签名问题

- 问题：Codex shell 缺少常用路径，导致 `npm`、`npx`、`gh`、`vercel` 等命令不可用；把 Codex.app 自带 Node 放入 PATH 后，`npm run lint` 又因 `rolldown` 原生模块 code signing / Team ID 不一致而失败。
- 原因：Codex 桌面进程的 PATH 与普通终端不同；`/opt/homebrew/bin` 未进入 PATH，Codex.app 的 `cua_node` 虽提供 `npm/npx`，但其 Node 进程加载项目里的 darwin arm64 原生 binding 时会触发 macOS 签名校验问题。
- 修正方式：新增 `~/.config/shell/path.zsh` 并由 `~/.zshenv`、`~/.zprofile` 加载；PATH 优先包含 `~/.local/bin`、Homebrew 路径、Codex runtime 的 `node/bin`，再包含 Codex.app 的 `npm/npx` 路径；把 GitHub CLI 常驻到 `~/.local/bin/gh`，新增 `~/.local/bin/vercel` shim。
- 以后避免原则：配置 PATH 后必须用真实项目命令验证，不只检查 `command -v`；对于包含原生依赖的 Node 项目，要确认实际执行的 `node` 二进制能加载 native binding。

## 2026-07-06 12:44 CST - Vercel Git 绑定需用真实 push 验证

- 问题：Vercel 项目需要绑定 GitHub 仓库，让后续 `git push` 自动触发部署；`vercel project inspect` 不直观展示 Git 连接字段，单靠 inspect 容易误判。
- 原因：Vercel CLI 的项目信息输出偏通用配置，Git 连接状态需要通过 `vercel git connect` 的结果和后续部署元数据确认。
- 修正方式：执行 `vercel git connect https://github.com/tianjiashu/buji-blog.git`，随后推送空提交 `Trigger Vercel git deployment`，用 Vercel 部署详情确认 `source: git`、`githubRepo: buji-blog`、`githubCommitRef: main`，并确认部署进入 `READY`。
- 以后避免原则：平台绑定类操作要做端到端验证；绑定 Git 后至少推一次无害提交，确认 Vercel 确实从 GitHub main 分支触发生产部署。

## 2026-07-06 12:51 CST - 走弯路的元类型与任务前检查

- 问题：多次弯路不只是单点错误，而是有重复模式：有工具但不会熟练使用、有环境限制但未先识别、有工具存在但没意识到优先使用。
- 原因：执行时容易从“能完成任务”的局部路径出发，而不是先建立工具、权限、环境、最佳实践四个维度的判断；一旦第一条路径受阻，就可能在未充分诊断的情况下改走次优路线。
- 修正方式：每次任务开始前，除阅读本文件外，先做轻量盘点：任务相关的首选工具/插件/CLI 是否存在；当前 shell、PATH、凭据、网络、沙箱、项目包管理器有什么限制；是否已有官方/项目内/插件提供的最佳路径；若工具不会用，先查 `--help`、技能说明或官方入口，不急着换方案。
- 以后避免原则：完成任务不是唯一目标，优先用长期可维护的最佳实践完成任务；遇阻时先诊断并说明，再修正首选路线，只有首选路线经有效尝试仍不可行时才切换替代路线，并记录取舍。

## 2026-07-06 12:55 CST - 协作提示词要强调复用和节省探索成本

- 问题：仅要求“记录踩坑”还不够，记录如果不被总结、压缩、复用，后续任务仍可能重复消耗 token 和时间。
- 原因：单条教训偏事件级，不能自动转化为任务前的执行策略；提示词需要明确要求从记录中提炼可迁移模式，并在新任务里优先复用成熟路径。
- 修正方式：协作指令应从“记录错误”升级为“读取记录 -> 提炼相关原则 -> 选择最佳路径 -> 遇阻诊断 -> 更新可复用教训”的闭环，并要求记录元类型、决策依据和可复用检查项。
- 以后避免原则：提示词保持精炼，但必须覆盖复盘、复用、最佳实践和 token 节省；每次新增教训时优先写成未来可执行的检查项，而不只是事故回放。

## 2026-07-06 15:53 CST - 构建验证与 Vercel 本地文件处理

- 问题：开发匿名留言板时并行执行 `npm run build` 与 `vercel build --yes`，两个进程同时写 `dist`，导致 Astro 偶发 `Duplicate export of 'manifest'`；另外 `vercel pull/build/dev` 会在 `.vercel/.env.*.local` 生成本地环境文件，可能包含临时 OIDC token。
- 原因：构建命令不是只读验证，会写同一输出目录；Vercel CLI 为本地模拟会拉取/生成环境文件，虽然 `.vercel` 被 gitignore，但仍应避免敏感临时文件长期留在工作区。
- 修正方式：改为串行执行 `npm run lint && npm run build && npx tsc --noEmit && vercel build --yes`；发现 `.vercel/.env.preview.local` 后立即删除；Vercel 安装命令固定为 `npm ci` 保持锁文件可复现。
- 以后避免原则：可并行的只读检查才并行；所有会写 `dist`、`.vercel/output`、`.astro` 的构建/预览命令应串行执行。运行 Vercel 本地命令后检查 `.vercel/.env*.local`，不要把敏感本地环境文件留存或提交。

## 2026-07-06 16:32 CST - `tsx -e` 验证脚本的 top-level await 限制

- 问题：用 `npx tsx -e "..."` 直接执行带 top-level await 的 API 验证脚本时，esbuild 报 `Top-level await is currently not supported with the "cjs" output format`。
- 原因：`tsx -e` 的内联脚本可能按 CommonJS 输出处理，顶层 await 不稳定；项目本身是 ESM，但内联执行模式不等同于项目模块文件。
- 修正方式：把内联验证逻辑包进 async IIFE，或改用临时 `.mts`/`.ts` 文件执行。
- 以后避免原则：需要用 `tsx -e` 快速验证异步模块时，默认写成 `(async () => { ... })()`；不要把这个工具限制误判为项目代码失败。

## 2026-07-06 16:52 CST - GitHub Issues 存储层边界要显式处理

- 问题：将留言存储迁移到 GitHub Issues 时，第一版只处理了基础读写，遗漏了 GitHub API 的分页、Issues 列表包含 Pull Request、数字 ID 不存在、非本站留言 comment 等边界；mock 脚本还因 HTML 注释里的 `!` 触发 zsh 历史展开。
- 原因：把 GitHub Issues 当成简单列表存储使用，容易忽视它本身是通用协作系统；API 返回模型、分页和错误码都需要显式映射。shell 内联脚本也会受 zsh 语法影响。
- 修正方式：用 GitHub Search API 按标题查文章 Issue，并排除 `pull_request`；评论列表分页读取并设置上限；GitHub 404 映射为“留言不存在”；非 Buji comment 共鸣返回 404；内联 mock 避免直接写 `<!--`，或用单引号/字符串拼接。
- 以后避免原则：把第三方平台当存储层时，必须检查分页、错误码、资源类型混入、权限错误和数据归属校验；mock 测试第三方 URL 时，避免用模糊 substring 匹配查询参数，优先用 URLSearchParams 或精确匹配。

## 2026-07-06 17:10 CST - Vercel CLI 交互式添加密钥的提示顺序

- 问题：执行 `vercel env add NAME production` 时，CLI 先询问是否作为 sensitive 保存，再询问变量值；直接把密钥写入 stdin 会被当作第一个问题的答案并在终端回显。
- 原因：误判了 Vercel CLI 交互提示顺序，把 “Value?” 当作第一个输入提示。
- 修正方式：先等待并回答 `Store as sensitive?`，再在 `Value?` 提示出现后输入密钥；或者优先使用非交互安全通道/管理后台粘贴密钥。
- 以后避免原则：向 CLI 写入任何 secret 前，必须先确认当前提示文本；不确定提示顺序时先用非敏感占位值演练或改用平台 UI。
