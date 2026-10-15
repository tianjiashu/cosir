/**
 * 系统外观同步：让 <html> 的 `.dark` 类与 `theme-color` 跟随操作系统浅色 / 深色。
 *
 * 首帧前的初值由 index.html 的内联脚本按同一媒体查询写入，避免深色系统下
 * 先闪一帧白底；本模块在应用启动时接管，并在系统外观切换时实时更新。
 * 目前只有「跟随系统」一种模式；以后加入用户偏好时只需在这里决定最终主题，
 * token 与组件都只认 `.dark` 类，不需要改动。
 */

const DARK_SCHEME_QUERY = "(prefers-color-scheme: dark)";
const LIGHT_THEME_COLOR = "#ffffff";
const DARK_THEME_COLOR = "#0a0a0a";

function applyTheme(dark: boolean): void {
  const root = document.documentElement;
  root.classList.toggle("dark", dark);
  document
    .querySelector<HTMLMetaElement>('meta[name="theme-color"]')
    ?.setAttribute("content", dark ? DARK_THEME_COLOR : LIGHT_THEME_COLOR);
}

/**
 * 立即按当前系统外观设置主题，并订阅后续变化。
 *
 * 只在应用入口调用一次；返回的函数用于测试或热更新时解除订阅。
 */
export function installSystemThemeSync(): () => void {
  const query = window.matchMedia(DARK_SCHEME_QUERY);
  applyTheme(query.matches);
  const handleChange = (event: MediaQueryListEvent) => applyTheme(event.matches);
  query.addEventListener("change", handleChange);
  return () => query.removeEventListener("change", handleChange);
}
