import path from "node:path";
import { app } from "electron";

/**
 * 宿主运行期图标的定位。位图由 `scripts/generate-icons.mjs` 从 `resources/icons/source/` 的 SVG 生成，
 * 构建时随 main 复制到 `__dirname/resources`（见 `scripts/build-electron.mjs`）。
 */
export function iconPath(name: string): string {
  return path.join(__dirname, "resources", "icons", name);
}

/**
 * 开发运行时的应用包仍携带 Electron 图标，由宿主在启动时换成 Cosir 图标。
 * `dock.png` 已按 Apple 图标网格留白，与其他应用等大。
 */
export function applyDockIcon(): void {
  if (process.platform === "darwin") app.dock?.setIcon(iconPath("dock.png"));
}
