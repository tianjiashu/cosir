/**
 * 启动入口冒烟：跑一个启动命令（`npm run electron:dev`、`scripts/start-desktop.ps1`），等后端 ready，再只强杀
 * Electron 主进程（模拟宿主崩溃），断言后端经宿主存活契约自行优雅退出（bootstate 落到 `stopped`，见
 * `apps/backend/app/parent_watch.py` 与 `lifespan.py`）。最后清掉启动命令留下的整棵进程树。
 *
 * 用法：`node scripts/verify-desktop-launch.mjs <命令> [参数...]`。数据根指到新建的临时目录
 * （`CODING_AGENT_DATA_DIR`），不碰真实数据；有后端虚拟环境时直接用它的解释器。
 *
 * 只给 CI 用：会打开真实窗口。
 */
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { desktopRoot, repositoryRoot } from "./project-paths.mjs";

const READY_TIMEOUT_MS = 300_000;
const STOP_TIMEOUT_MS = 30_000;
const isWindows = process.platform === "win32";

const [command, ...args] = process.argv.slice(2);
if (!command) {
  console.error("usage: node scripts/verify-desktop-launch.mjs <command> [args...]");
  process.exit(2);
}

const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "cosir-launch-smoke-"));
const bootstateFile = path.join(dataDir, ".cosir", "runtime", "backend.bootstate.json");
const backendPython = path.join(repositoryRoot, "apps", "backend", isWindows ? ".venv/Scripts/python.exe" : ".venv/bin/python");

// Windows 上 .cmd 不经 shell 起不来（Node 拒绝直接 spawn 批处理）。
const launcher = spawn(command, args, {
  cwd: desktopRoot,
  stdio: "inherit",
  shell: isWindows && command.toLowerCase().endsWith(".cmd"),
  detached: !isWindows,
  env: {
    ...process.env,
    CODING_AGENT_DATA_DIR: dataDir,
    ...(fs.existsSync(backendPython) ? { COSIR_BACKEND_PYTHON: backendPython } : {}),
  },
});
let launcherExit = null;
launcher.on("exit", (code, signal) => {
  launcherExit = { code, signal };
});

try {
  await waitFor("backend ready", READY_TIMEOUT_MS, () => {
    if (launcherExit) throw new Error(`launcher exited before the backend was ready: ${JSON.stringify(launcherExit)}`);
    return bootstatePhase() === "ready";
  });
  const electronMain = electronMainProcess(launcher.pid);
  const backend = backendProcess(electronMain);
  console.log(`[launch-smoke] backend ready (pid ${backend}); killing only the Electron main process ${electronMain}`);
  const killedAt = Date.now();
  killOne(electronMain);
  try {
    await waitFor("backend stopped after the host was killed", STOP_TIMEOUT_MS, () => bootstatePhase() === "stopped");
  } finally {
    // 无论成败都报出后端什么时候退出的：没写 stopped 时，用来区分「优雅关闭卡住、15 秒后被强制退出」与「一直没退」。
    await waitFor("backend process exit", STOP_TIMEOUT_MS, () => !isAlive(backend)).catch(() => {});
    const exited = isAlive(backend) ? "still alive" : `exited within ${Math.round((Date.now() - killedAt) / 1000)}s`;
    console.log(`[launch-smoke] backend ${backend} ${exited}; bootstate ${bootstatePhase() ?? "missing"}`);
  }
  console.log("[launch-smoke] backend shut down gracefully after the host disappeared");
} catch (error) {
  console.error(`[launch-smoke] ${error.message}`);
  printLogs();
  process.exitCode = 1;
} finally {
  killTree(launcher.pid);
}

function bootstatePhase() {
  try {
    return JSON.parse(fs.readFileSync(bootstateFile, "utf8")).phase;
  } catch {
    return null;
  }
}

async function waitFor(what, timeoutMs, check) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (check()) return;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`timed out waiting for ${what} (bootstate: ${bootstatePhase() ?? "missing"})`);
}

/** 启动命令的后代里最上层的 Electron 进程（渲染、GPU 等辅助进程是它的子进程）。 */
function electronMainProcess(rootPid) {
  const processes = processTable();
  const byPid = new Map(processes.map((item) => [item.pid, item]));
  const descendants = new Set([rootPid]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const item of processes) {
      if (!descendants.has(item.pid) && descendants.has(item.ppid)) {
        descendants.add(item.pid);
        grew = true;
      }
    }
  }
  const isElectron = (item) => item !== undefined && /electron/i.test(item.name);
  const main = processes.find((item) => descendants.has(item.pid) && isElectron(item) && !isElectron(byPid.get(item.ppid)));
  if (!main) throw new Error(`no Electron process under the launcher ${rootPid}`);
  return main.pid;
}

/** Electron 主进程的子进程里的后端（开发期是 Python 解释器，发布版是 cosir-backend）。 */
function backendProcess(electronMain) {
  const backend = processTable().find((item) => item.ppid === electronMain && /python|cosir-backend/i.test(item.name));
  if (!backend) throw new Error(`no backend process under the Electron main process ${electronMain}`);
  return backend.pid;
}

function isAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function processTable() {
  if (isWindows) {
    const json = execFileSync(
      "powershell.exe",
      ["-NoLogo", "-NoProfile", "-Command", "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name | ConvertTo-Json -Compress"],
      { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 },
    );
    return JSON.parse(json).map((item) => ({ pid: item.ProcessId, ppid: item.ParentProcessId, name: item.Name ?? "" }));
  }
  return execFileSync("ps", ["-A", "-o", "pid=,ppid=,comm="], { encoding: "utf8" })
    .split("\n")
    .map((line) => line.trim().match(/^(\d+)\s+(\d+)\s+(.*)$/))
    .filter(Boolean)
    .map((match) => ({ pid: Number(match[1]), ppid: Number(match[2]), name: path.basename(match[3]) }));
}

function killOne(pid) {
  if (isWindows) execFileSync("taskkill", ["/PID", String(pid), "/F"], { stdio: "ignore" });
  else process.kill(pid, "SIGKILL");
}

function killTree(pid) {
  try {
    if (isWindows) execFileSync("taskkill", ["/PID", String(pid), "/T", "/F"], { stdio: "ignore" });
    else process.kill(-pid, "SIGKILL");
  } catch {
    // 已经全部退出。
  }
}

function printLogs() {
  const logDir = path.join(dataDir, ".cosir", "logs");
  if (!fs.existsSync(logDir)) return;
  for (const name of fs.readdirSync(logDir)) {
    if (!name.endsWith(".log")) continue;
    const lines = fs.readFileSync(path.join(logDir, name), "utf8").trim().split("\n");
    console.error(`----- ${name} (last 120 lines) -----\n${lines.slice(-120).join("\n")}`);
  }
}
