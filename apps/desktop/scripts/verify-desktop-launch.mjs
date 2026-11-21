/**
 * 启动入口冒烟：跑一个启动命令（`npm run electron:dev`、`scripts/start-desktop.*`），等后端 ready，再只强杀
 * Electron 主进程（模拟宿主崩溃），断言宿主之下的进程（后端及其子进程）30 秒内全部退出、不留孤儿。最后清掉启动命令
 * 留下的整棵进程树。
 *
 * 两个平台的退出方式不同，断言按平台分：
 * - POSIX：后端是独立进程组，宿主死后读到 stdin EOF，经宿主存活契约优雅关闭（bootstate 落到 `stopped`，见
 *   `apps/backend/app/parent_watch.py` 与 `lifespan.py`）。
 * - Windows：Node（libuv）把非 detached 的子进程放进一个随父进程关闭即结束全部成员的 Job Object，宿主一死后端连同
 *   它的子进程被系统直接结束，不走优雅关闭，bootstate 停在 `ready`；只断言不留孤儿。
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
  // 杀之前记下宿主之下的整棵树（后端、Python 启动器、Worker 等），之后逐一报它们什么时候退出。
  const tree = descendantsOf(electronMain);
  const backend = [...tree].find(([, name]) => /python|cosir-backend/i.test(name));
  if (!backend) throw new Error(`no backend process under the Electron main process ${electronMain}: ${JSON.stringify([...tree])}`);
  console.log(`[launch-smoke] backend ready; host tree ${JSON.stringify([...tree])}; killing only the Electron main process ${electronMain}`);
  const killedAt = Date.now();
  killOne(electronMain);
  const exitedAt = new Map();
  await waitFor("the host's process tree to exit", STOP_TIMEOUT_MS, () => {
    for (const [pid] of tree) if (!exitedAt.has(pid) && !isAlive(pid)) exitedAt.set(pid, ((Date.now() - killedAt) / 1000).toFixed(1));
    return exitedAt.size === tree.size && (isWindows || bootstatePhase() === "stopped");
  }).finally(() => {
    const report = [...tree].map(([pid, name]) => `${name}#${pid}: ${exitedAt.has(pid) ? `exited after ${exitedAt.get(pid)}s` : "still alive"}`);
    console.log(`[launch-smoke] after the host was killed: ${report.join(", ")}; bootstate ${bootstatePhase() ?? "missing"}`);
  });
  console.log(
    isWindows
      ? "[launch-smoke] the backend and everything under it ended with the host (Node's job object), nothing left behind"
      : "[launch-smoke] the backend shut down gracefully after the host disappeared, nothing left behind",
  );
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
  // 只认可执行文件名：npm 把进程标题改成「npm run electron:dev」，按子串认会把 npm 当成 Electron。
  const isElectron = (item) => item !== undefined && /^electron(\.exe)?$/i.test(item.name);
  const main = processes.find((item) => descendants.has(item.pid) && isElectron(item) && !isElectron(byPid.get(item.ppid)));
  if (!main) throw new Error(`no Electron process under the launcher ${rootPid}`);
  return main.pid;
}

/** 某进程之下的全部后代：PID → 可执行文件名（Electron 的辅助进程除外）。 */
function descendantsOf(rootPid) {
  const processes = processTable();
  const found = new Map();
  let grew = true;
  while (grew) {
    grew = false;
    for (const item of processes) {
      if (!found.has(item.pid) && (item.ppid === rootPid || found.has(item.ppid)) && !/^electron(\.exe)?$|helper/i.test(item.name)) {
        found.set(item.pid, item.name);
        grew = true;
      }
    }
  }
  return found;
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
