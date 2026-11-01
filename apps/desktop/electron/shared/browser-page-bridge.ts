/**
 * 内置浏览器网页与宿主之间的桥：网页 preload（`electron/preload/browser-page.ts`）与 `BrowserTab` 共用。
 *
 * 只有 JS 弹窗走这里。Electron 没有拦截弹窗的公开钩子，而它默认的弹窗处理在标签不挂在窗口上时会卡住主进程
 * （实测，见设计稿），所以由 preload 在页面主世界里接管 `alert` / `confirm` / `prompt`，经同步 IPC 等宿主
 * 作答；作答前页面线程停在这里，与原生弹窗的语义一致。
 *
 * 本文件只放常量与类型：preload 在沙箱里单独打包，不能带上 main 的模块。
 */

export const BROWSER_PAGE_DIALOG_CHANNEL = "cosir:browser-page.dialog";

/** preload 交给宿主的弹窗请求；内容来自网页，宿主收到后照不可信输入处理。 */
export type BrowserPageDialogRequest = { type: string; message: string; defaultText: string };

/** 宿主的答复，即弹窗函数的返回值：alert 为 null，confirm 为布尔，prompt 为字符串或 null（取消）。 */
export type BrowserPageDialogReply = boolean | string | null;
