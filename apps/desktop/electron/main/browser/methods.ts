import type { HostMethodRegistry } from "../host-link/methods";
import type { BrowserHost } from "./browser-host";
import { parseAct, parseDropTasks, parseNavigate, parseScreenshot, parseSnapshot, parseTabs } from "./params";

/**
 * 把内置浏览器注册成宿主工具链路的方法。入参在这里经 `params.ts` 校验后交给 `BrowserHost`。
 *
 * - `browser.navigate`：要求新标签或 Task 没有标签时新开一个；返回导航后的快照。
 * - `browser.snapshot`：读当前（或指定）标签的快照，可从 `from_line` 起分页读。
 * - `browser.act`：按序执行动作，返回执行情况与之后的快照。
 * - `browser.screenshot`：截图（data URL），由后端存成附件交给模型。
 * - `browser.tabs`：列出 / 切换 / 关闭本 Task 的标签。
 * - `browser.dropTasks`：通知，Task 删除提交后回收它的全部标签。
 */
export function registerBrowserMethods(methods: HostMethodRegistry, browser: BrowserHost): void {
  methods.register("browser.navigate", (params, { signal }) => browser.navigate(parseNavigate(params), signal));
  methods.register("browser.snapshot", async (params, { signal }) => browser.snapshot(parseSnapshot(params), signal));
  methods.register("browser.act", (params, { signal }) => browser.act(parseAct(params), signal));
  methods.register("browser.screenshot", async (params, { signal }) => browser.screenshot(parseScreenshot(params), signal));
  methods.register("browser.tabs", async (params) => browser.manageTabs(parseTabs(params)));
  methods.register("browser.dropTasks", async (params) => {
    browser.dropTasks(parseDropTasks(params));
  });
}
