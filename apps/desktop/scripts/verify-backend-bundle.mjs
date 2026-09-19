import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const backendDir = path.join(desktopRoot, "src-tauri", "resources", "backend");
const executableName = process.platform === "win32" ? "cosir-backend.exe" : "cosir-backend";
const executablePath = path.join(backendDir, executableName);
const temporaryRoot = fs.mkdtempSync(path.join(os.tmpdir(), "cosir-backend-smoke-"));

function reservePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (!address || typeof address === "string") {
        server.close();
        reject(new Error("无法为后端冒烟检查分配本地端口"));
        return;
      }
      server.close((error) => (error ? reject(error) : resolve(address.port)));
    });
  });
}

function minimalEnvironment(port) {
  const inheritedNames = [
    "SystemRoot",
    "WINDIR",
    "PATH",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
  ];
  const environment = Object.fromEntries(
    inheritedNames
      .filter((name) => process.env[name] !== undefined)
      .map((name) => [name, process.env[name]]),
  );
  const runtimeDir = path.join(temporaryRoot, "runtime");
  const dataDir = path.join(temporaryRoot, "data");
  fs.mkdirSync(runtimeDir, { recursive: true });
  fs.mkdirSync(dataDir, { recursive: true });
  return {
    ...environment,
    CODING_AGENT_PORT: String(port),
    CODING_AGENT_RELOAD: "false",
    CODING_AGENT_DATA_DIR: dataDir,
    CODING_AGENT_LOG_DIR: path.join(runtimeDir, "logs"),
    CODING_AGENT_BOOT_STATE_FILE: path.join(runtimeDir, "backend.bootstate.json"),
    CODING_AGENT_SQLITE_LOGGING_ENABLED: "false",
  };
}

function appendTail(current, chunk) {
  return `${current}${chunk}`.slice(-8000);
}

async function waitForBackend(child, port, output) {
  const deadline = Date.now() + 120_000;
  let lastError = "尚未收到后端健康响应";
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`冻结后端提前退出，exit code=${child.exitCode}\n${output()}`);
    }
    try {
      const response = await fetch(`http://127.0.0.1:${port}/health`, {
        signal: AbortSignal.timeout(1500),
      });
      if (response.ok) {
        const body = await response.json();
        if (body.status !== "health") {
          throw new Error(`后端 /health 返回了意外内容：${JSON.stringify(body)}`);
        }
        return;
      }
      lastError = `后端 /health 返回 HTTP ${response.status}`;
    } catch (error) {
      lastError = error.message;
    }
    await new Promise((resolve) => setTimeout(resolve, 350));
  }
  throw new Error(`冻结后端在 120 秒内未就绪：${lastError}\n${output()}`);
}

function waitForExit(child, timeoutMs) {
  if (child.exitCode !== null) return Promise.resolve();
  return Promise.race([
    new Promise((resolve) => child.once("exit", resolve)),
    new Promise((_, reject) =>
      setTimeout(() => reject(new Error("冻结后端冒烟进程未能停止")), timeoutMs),
    ),
  ]);
}

async function main() {
  if (!fs.existsSync(executablePath)) {
    throw new Error(`找不到待验收的后端可执行文件：${executablePath}`);
  }
  const providerCheck = spawn(executablePath, ["--packaging-check"], {
    cwd: backendDir,
    env: {
      ...minimalEnvironment(0),
      LITELLM_LOCAL_MODEL_COST_MAP: "true",
    },
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  let providerOutput = "";
  let providerError = "";
  providerCheck.stdout.on("data", (chunk) => {
    providerOutput = appendTail(providerOutput, chunk);
  });
  providerCheck.stderr.on("data", (chunk) => {
    providerError = appendTail(providerError, chunk);
  });
  const providerExitCode = await new Promise((resolve, reject) => {
    providerCheck.once("error", reject);
    providerCheck.once("exit", (code) => resolve(code));
  });
  if (providerExitCode !== 0 || !providerOutput.includes("frozen provider import check passed")) {
    throw new Error(
      `冻结后端未能解析动态模型厂商模块，exit code=${providerExitCode}\n${providerOutput}\n${providerError}`,
    );
  }

  const port = await reservePort();
  let stdout = "";
  let stderr = "";
  const child = spawn(executablePath, [], {
    cwd: backendDir,
    env: minimalEnvironment(port),
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  child.stdout.on("data", (chunk) => {
    stdout = appendTail(stdout, chunk);
  });
  child.stderr.on("data", (chunk) => {
    stderr = appendTail(stderr, chunk);
  });
  child.once("error", (error) => {
    stderr = appendTail(stderr, `\n${error.message}`);
  });

  try {
    await waitForBackend(child, port, () => `stdout:\n${stdout}\nstderr:\n${stderr}`);
    const database = path.join(temporaryRoot, "data", "storage", "app.sqlite3");
    if (!fs.existsSync(database)) {
      throw new Error(`后端没有在用户数据目录创建主数据库：${database}`);
    }
    child.kill();
    await waitForExit(child, 10_000);
    console.log("冻结后端冒烟检查通过：可启动、/health 正常、SQLite 位于用户数据目录。");
  } finally {
    if (child.exitCode === null) {
      child.kill();
      await waitForExit(child, 10_000).catch(() => {});
    }
    const tempRoot = path.resolve(os.tmpdir());
    const relative = path.relative(tempRoot, temporaryRoot);
    if (!relative.startsWith("cosir-backend-smoke-") || path.isAbsolute(relative)) {
      throw new Error(`拒绝清理临时目录之外的路径：${temporaryRoot}`);
    }
    fs.rmSync(temporaryRoot, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
