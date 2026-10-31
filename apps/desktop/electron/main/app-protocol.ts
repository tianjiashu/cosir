import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { app, net, protocol } from "electron";

/** 构建产物的页面来源；后端 CORS 白名单必须包含它。 */
export const APP_ORIGIN = "app://cosir";

/**
 * 构建产物的前端目录。开发期（未打包）读仓库 `target/frontend`（vite build 的输出），
 * 打包后的位置由 P2 的打包配置决定并在这里切换。
 */
function frontendDir(): string {
  return app.isPackaged
    ? path.join(process.resourcesPath, "frontend")
    : path.resolve(__dirname, "..", "frontend");
}

/** 必须在 `app.ready` 之前调用：特权 scheme 只能在启动早期注册。 */
export function registerAppScheme(): void {
  protocol.registerSchemesAsPrivileged([
    { scheme: "app", privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true } },
  ]);
}

/**
 * 以 `app://cosir/` 提供构建产物，而不是直接用 `file://`：页面有稳定的来源（CORS、存储都按它隔离），
 * 绝对路径的静态资源也能解析。路由用 BrowserRouter，找不到对应文件的路径都回落到 `index.html`。
 * 解析后的路径必须仍在前端目录内，防止 `..` 越出。
 */
export function handleAppProtocol(): void {
  const root = frontendDir();
  protocol.handle("app", (request) => {
    const requested = path.normalize(path.join(root, decodeURIComponent(new URL(request.url).pathname)));
    const inside = requested === root || requested.startsWith(`${root}${path.sep}`);
    const file = inside && fs.existsSync(requested) && fs.statSync(requested).isFile()
      ? requested
      : path.join(root, "index.html");
    return net.fetch(pathToFileURL(file).toString());
  });
}
