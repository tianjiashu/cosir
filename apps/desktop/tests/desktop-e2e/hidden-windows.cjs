/**
 * 桌面 E2E 的无窗口启动钩子（经 Electron 的 `-r` 在宿主主进程代码之前加载，见 `desktop-app.ts`）：
 * 让宿主建出的每个 `BrowserWindow` 都不显示、Dock 图标隐藏，开发者在同一台电脑上工作时不被反复弹出的窗口打扰。
 * 产品代码不知道它的存在：`electron` 模块的导出是不可改写的 getter，所以在模块加载处换成一个代理，只拦截
 * `new BrowserWindow(...)`。不能用子类：`BrowserWindow.getAllWindows()` / `fromWebContents()` 认不出子类实例，宿主的
 * 后端状态广播与 IPC 来源校验会因此失效；构造代理返回的就是原生实例。隐藏窗口默认会节流渲染，测试要看布局与
 * 原生视图摆放，因此关掉。
 */
const Module = require("node:module");

const load = Module._load;
let patched;
Module._load = function (request, ...rest) {
  const real = load.call(this, request, ...rest);
  if (request !== "electron" || !real.BrowserWindow) return real;
  if (!patched) {
    const HiddenBrowserWindow = new Proxy(real.BrowserWindow, {
      construct(target, [options = {}]) {
        const window = Reflect.construct(target, [{ ...options, show: false, webPreferences: { ...options.webPreferences, backgroundThrottling: false } }]);
        window.show = () => undefined;
        window.focus = () => undefined;
        return window;
      },
    });
    patched = new Proxy(real, { get: (target, key) => (key === "BrowserWindow" ? HiddenBrowserWindow : Reflect.get(target, key)) });
    void real.app.whenReady().then(() => real.app.dock?.hide());
  }
  return patched;
};
