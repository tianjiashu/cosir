import { defineConfig, type UserConfigFnObject } from "vite";

import baseConfig from "./vite.config";

/**
 * E2E 拥有的前端服务（`playwright.config.ts` 的 webServer 用它）：与开发配置相同，只是不监听文件、不推热更新。
 *
 * E2E 跑的是开发者正在改的同一份工作区。开着监听时，运行中任何一次保存（本人、其他会话或 Agent；macOS 的文件
 * 事件还会延迟几十秒成批送达）都会让打开的测试页热更新，改到不能热替换的模块时整页重载——跨重载断言 DOM 节点、
 * 选区、焦点的用例随之偶发失败，且失败的用例每轮不同。测试应当跑启动那一刻的代码，改了代码重跑即可。
 *
 * 不改用 `vite build` + `preview`：用例依赖开发模式的行为（StrictMode 双挂载、`import.meta.env.DEV` 下的探针）。
 * `watch: null` 必须直接覆盖：Vite 的 `mergeConfig` 会跳过 null 值。
 */
export default defineConfig((env) => {
  const config = (baseConfig as UserConfigFnObject)(env);
  return { ...config, server: { ...config.server, hmr: false, watch: null } };
});
