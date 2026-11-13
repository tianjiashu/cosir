import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import type { Logger, Plugin } from "vite";

const CACHE_ROOT = path.resolve(__dirname, "..", "node_modules", ".vite");
const PORT_DIRECTORY = /^port-(\d+)$/;
/** 闲置超过这么久、且端口没有进程在监听的缓存目录才回收；常用端口重启时仍能沿用热缓存。 */
const IDLE_MS = 7 * 24 * 60 * 60 * 1000;
const PROBE_TIMEOUT_MS = 300;

/**
 * 开发服务器按端口各用一个依赖预构建缓存目录（`node_modules/.vite/port-<端口>`），并回收闲置的目录。
 *
 * 同一项目常同时跑多个 Vite（Electron 开发 3000、各 E2E 槽位 4173+、预览服务器），共用默认的
 * `node_modules/.vite` 时，后启动或重新预构建的进程会覆盖别人的 `deps/`：先前的进程仍按内存里的
 * 文件名提供模块，页面里懒加载的依赖分块（Mermaid、代码高亮）请求到已被换掉的文件，得到 504，
 * 消息渲染失败。所有启动方都带 `--strictPort`，端口即进程身份，按端口分目录就不需要各启动方
 * 记得传参；同一端口重启时沿用自己的缓存。插件的 `config` 钩子拿到的是已合并命令行参数的配置。
 *
 * 端口不是封闭集合（E2E slot 任取、临时 `--port`），每份缓存约百兆，只建不收会无限累积，所以每次
 * 启动都顺带回收：启动时先把自己的目录 mtime 刷新为「最近使用」，再删掉闲置超过 `IDLE_MS`、且端口
 * 当前无人监听的兄弟目录。端口探测防的是长时间运行、mtime 早已过期的服务器被删掉缓存（又回到 504）；
 * 刚启动还没开始监听的服务器已在 `config` 里刷新过 mtime，不会被并发启动的另一方误删。回收在后台进行、
 * 不拖慢启动，失败只记日志。Vitest（mode `test`）与 `vite preview` 不做依赖预构建，不启用本插件。
 */
export function devServerCacheDirPlugin(defaultPort: number): Plugin {
  let port = defaultPort;
  return {
    name: "cosir:dev-server-cache-dir",
    apply: (_config, env) => env.command === "serve" && !env.isPreview && env.mode !== "test",
    config(config) {
      port = config.server?.port ?? defaultPort;
      const cacheDir = path.join(CACHE_ROOT, `port-${port}`);
      fs.mkdirSync(cacheDir, { recursive: true });
      const now = new Date();
      fs.utimesSync(cacheDir, now, now);
      return { cacheDir };
    },
    configureServer(server) {
      const logger = server.config.logger;
      void reclaimIdleCacheDirs(CACHE_ROOT, port, logger).catch((error: unknown) => {
        logger.warn(`[cosir] 回收 Vite 缓存目录失败：${String(error)}`, { timestamp: true });
      });
    },
  };
}

/** 删除 `cacheRoot` 下闲置且端口无人监听的 `port-*` 目录（`ownPort` 除外），返回被删的目录名。 */
export async function reclaimIdleCacheDirs(cacheRoot: string, ownPort: number, logger: Pick<Logger, "info" | "warn">): Promise<string[]> {
  const cutoff = Date.now() - IDLE_MS;
  const candidates = fs.readdirSync(cacheRoot, { withFileTypes: true }).flatMap((entry) => {
    const match = entry.isDirectory() ? PORT_DIRECTORY.exec(entry.name) : null;
    if (!match || Number(match[1]) === ownPort) return [];
    const directory = path.join(cacheRoot, entry.name);
    return fs.statSync(directory).mtimeMs < cutoff ? [{ name: entry.name, directory, port: Number(match[1]) }] : [];
  });
  const reclaimed: string[] = [];
  for (const candidate of candidates) {
    if (await isPortListening(candidate.port)) continue;
    try {
      fs.rmSync(candidate.directory, { recursive: true, force: true });
      reclaimed.push(candidate.name);
    } catch (error) {
      logger.warn(`[cosir] 无法删除闲置 Vite 缓存 ${candidate.name}：${String(error)}`, { timestamp: true });
    }
  }
  if (reclaimed.length > 0) logger.info(`[cosir] 已回收闲置 Vite 缓存：${reclaimed.join(", ")}`, { timestamp: true });
  return reclaimed;
}

/** 所有启动方都绑定 127.0.0.1，探测同一地址即可。 */
function isPortListening(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = net.connect({ host: "127.0.0.1", port });
    const settle = (listening: boolean) => {
      socket.destroy();
      resolve(listening);
    };
    socket.setTimeout(PROBE_TIMEOUT_MS, () => settle(false));
    socket.once("connect", () => settle(true));
    socket.once("error", () => settle(false));
  });
}
