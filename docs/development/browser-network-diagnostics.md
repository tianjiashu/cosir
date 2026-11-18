# 内置浏览器网络诊断

常驻的诊断旁路，用来排查「网页在内置浏览器里不正常、在 Chrome 里正常」一类问题。只落本机日志，不进 Agent 上下文、不作为工具结果返回。代码在 `apps/desktop/electron/main/browser/diagnostics/`。

## 三份记录

| 记录 | 何时有 | 落在哪 | 回答什么 |
| --- | --- | --- | --- |
| 标签网络（`tab-network.ts`） | 常驻，每个标签 | `.cosir/logs/browser-network-YYYY-MM-DD.log` | 请求发没发、状态码、取消/CORS/阻挡原因、带了哪些 Cookie（只有名字）、连接是否复用、远端地址与 timing |
| Cookie 变化（`cookie-changes.ts`） | 常驻，整个分区 | 同上（`browser_cookie_changed`） | Cookie 存进去之后被覆盖、过期、挤出还是删除（`cause` / `removed`） |
| 原生 NetLog（`netlog-capture.ts`） | 手动：应用菜单「诊断 → 开始记录浏览器网络（15 分钟）」 | `.cosir/logs/browser-netlog-<UTC 时间>.json` | 挂住的请求卡在代理、DNS、连接池还是握手 |

- 标签网络只看开了 Network 的主会话：跨进程子框架、Worker、Service Worker 自己发的请求不在其中。图片、媒体、字体、样式只在失败时记（带地址），否则视频分片会在几小时内把当日日志轮转满。
- 都不记 Cookie 值、认证头、正文与 URL 查询参数。NetLog 的 `default` 模式不采 Cookie，但 URL 仍可能含参数，原始文件只在本机分析。
- 宿主日志按日命名、单文件 5 MiB、同日最多 8 份；跨天不清理。NetLog 每次一个新文件，至多 15 分钟或 20 MiB，退出时至多等 5 秒收尾；强杀可能留下没有尾部的文件。

## 页面落定日志

导航与每个动作之后，宿主按两层判定页面是否落定：这次操作之后、可能带内容的请求都回来，且快照大纲前后两次读取相同（`electron/main/browser/page-loading.ts` 的 `untilPageSettled`）。请求按起因认、不按大小认（`tab-log.ts`）：页面在没有操作时（上次操作或用户输入之后超过 3 秒）自己请求过的接口，或同一步里请求了两次以上的接口，算作页面的后台流量（埋点、轮询），之后不再等；换页即忘。每次落定都在 `desktop-*.log` 写一条 `browser_page_settled`，记录哪一层收尾（`stable` / `requests` / `changing` / `unreadable`）、等了多久、读了几次、忽略了几个后台请求（`background_ignored`）。某类页面老是等满上限，从这里能看出来。

## 复现时怎么用

1. 出现问题时**先不要重启应用**：重启会换掉网络进程和连接池，现场随之消失。
2. 菜单「诊断 → 开始记录浏览器网络」，然后在内置浏览器里复现一次，再「停止记录」。
3. 「诊断 → 打开日志文件夹」，对照同一时间段的 `browser-network` 日志与 NetLog。

## 已知案例：Service Worker 停止后导航卡死（2026-11-18 查明，Electron 45 修复）

现象：网站的 Service Worker 空闲被停掉（约 30 秒）或刚启动应用时，导航经过一次跳转进入它的作用范围，就停在「请求已排队、请求头没发出」，直到 30 秒时限报 `NAVIGATION_FAILED`；受控页面的子资源同样会成批卡十几秒。小红书直达搜索页（先 301）必现，闲置后进首页偏慢。

根因：Chromium 152（Electron 44）的 `ServiceWorkerAutoPreload` 缺陷（上游 559592113）：浏览器在启动 Worker 的同时并行发出请求，跳转先于 fetch 处理器回来时，加载器不把跳转交还给导航。NetLog 上表现为 `URL_REQUEST_DELEGATE_RECEIVED_REDIRECT` 一直不结束，与网络和代理无关。Chromium 151 / Electron 44 可复现，Chrome 157 与 Electron 45.3.4（Chromium 156）不再出现；关闭该特性也能消除。原生验收的「a redirect into a stopped service worker's scope」场景守着它（fetch 处理器不能写成空函数，Chromium 会直接跳过空处理器）。

此前「扫码登录后仍是游客」（确认请求约 6 秒被页面取消、`web_session` 没下发）与这个现象一致，但没有单独复现过；升级后若再出现，按上面的步骤采 NetLog。

## 验证

单测：`npx vitest run tests/unit/browser-diagnostics.test.ts tests/unit/app-menu.test.ts`。真实 Chromium 验收：`node scripts/test-browser-native.mjs`，覆盖 XHR timeout、CORS 预检失败、Cookie 元数据与变化、手动 NetLog 采集以及凭据过滤。
