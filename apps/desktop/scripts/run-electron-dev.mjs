import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import net from "node:net";
import path from "node:path";
import * as esbuild from "esbuild";

import { copyElectronResources, electronBuildOptions } from "./build-electron.mjs";
import { buildDevelopmentTerminalWorker } from "./cargo-target.mjs";
import { desktopRoot } from "./project-paths.mjs";

const RENDERER_HOST = "127.0.0.1";
const RENDERER_PORT = 3000;
const rendererUrl = `http://${RENDERER_HOST}:${RENDERER_PORT}`;
const require = createRequire(import.meta.url);
// electron 包在 Node 里导出的就是本机 Electron 可执行文件路径。
const electronExecutable = require("electron");
// 直接用 node 跑 vite 的命令行入口：经 npm / npm.cmd 转一层时，Windows 上不带 shell 无法启动 .cmd，
// 结束时也只杀得掉 npm、杀不到 vite 本身。vite 的 exports 不暴露 bin，从包入口（dist/node）回推。
const viteCli = path.resolve(path.dirname(require.resolve("vite")), "..", "..", "bin", "vite.js");

/**
 * 开发期桌面启动入口：先构建 Terminal Worker 并把路径注入宿主（宿主与后端都不猜测 Worker 位置），
 * 再由 Vite 提供渲染进程（热替换），esbuild 监听 main 与 preload，它们一变就重启 Electron（后端随宿主重启）。
 *
 * 用户关闭 Electron（不是因重新构建而重启）即结束整个开发会话；任一子进程退出或收到中断信号时一并清理。
 */
let electron = null;
let context = null;
let restarting = false;
let shuttingDown = false;

let terminalWorker;
try {
  terminalWorker = buildDevelopmentTerminalWorker();
} catch (error) {
  console.error(`[cosir] ${error.message}`);
  process.exit(1);
}

const vite = spawn(process.execPath, [viteCli, "--host", RENDERER_HOST, "--port", String(RENDERER_PORT), "--strictPort"], {
  cwd: desktopRoot,
  stdio: "inherit",
});
vite.on("exit", (code) => shutdown(code ?? 1));

await waitForPort(RENDERER_HOST, RENDERER_PORT);
copyElectronResources();

context = await esbuild.context({
  ...electronBuildOptions,
  plugins: [{
    name: "restart-electron",
    setup(build) {
      build.onEnd((result) => {
        if (result.errors.length === 0) restartElectron();
      });
    },
  }],
});
await context.watch();

function restartElectron() {
  if (shuttingDown) return;
  if (electron) {
    restarting = true;
    electron.once("exit", () => {
      restarting = false;
      startElectron();
    });
    electron.kill();
    return;
  }
  startElectron();
}

function startElectron() {
  electron = spawn(electronExecutable, ["."], {
    cwd: desktopRoot,
    stdio: "inherit",
    env: { ...process.env, COSIR_RENDERER_URL: rendererUrl, CODING_AGENT_TERMINAL_WORKER: terminalWorker },
  });
  electron.on("exit", (code) => {
    electron = null;
    if (!restarting) shutdown(code ?? 0);
  });
}

async function shutdown(code) {
  if (shuttingDown) return;
  shuttingDown = true;
  electron?.kill();
  await context?.dispose();
  vite.kill();
  process.exit(code);
}

process.on("SIGINT", () => shutdown(130));
process.on("SIGTERM", () => shutdown(143));

function waitForPort(host, port) {
  return new Promise((resolve) => {
    const attempt = () => {
      const socket = net.connect(port, host);
      socket.once("connect", () => {
        socket.end();
        resolve();
      });
      socket.once("error", () => setTimeout(attempt, 200));
    };
    attempt();
  });
}
