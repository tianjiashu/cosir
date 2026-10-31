import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { terminalWorkerExecutableName } from "./cargo-target.mjs";
import { repositoryRoot, targetDirectory } from "./project-paths.mjs";

const targetTriple = (
  process.env.COSIR_TERMINAL_WORKER_TARGET || ""
).trim();
const executableName = terminalWorkerExecutableName(targetTriple);
const instanceId = `bundle-smoke-${Date.now()}`;
// 安装包形态的验证随 P2 打包一起补；现在只验证 stage 产物（可用 COSIR_TERMINAL_RESOURCE_DIR 指向别处）。
const resourceRoot = path.resolve(process.env.COSIR_TERMINAL_RESOURCE_DIR || path.join(targetDirectory, "resources"));
const workerPath = path.join(resourceRoot, "terminal-worker", executableName);

assert.equal(fs.existsSync(workerPath), true, `Terminal Worker resource 不存在：${workerPath}`);
if (process.platform !== "win32") {
  assert.equal(fs.statSync(workerPath).mode & 0o111, 0o111, "Terminal Worker 没有执行权限");
}

/** 控制帧与事件帧的结构以 `apps/terminal-worker/src/protocol.rs` 为准（4 字节大端长度 + JSON）；协议变更时须同步这里。 */
function frame(message) {
  const payload = Buffer.from(JSON.stringify(message), "utf8");
  const header = Buffer.alloc(4);
  header.writeUInt32BE(payload.length, 0);
  return Buffer.concat([header, payload]);
}

function shellSpec() {
  if (process.platform === "win32") {
    const executable = process.env.ComSpec || "cmd.exe";
    return {
      shell: [executable, "/Q", "/D"],
      input: Buffer.from("echo terminal-worker-bundle-smoke\r\nexit\r\n"),
    };
  }
  const executable = process.env.SHELL || "/bin/sh";
  return {
    shell: [executable],
    input: Buffer.from("printf 'terminal-worker-bundle-smoke\\n'; exit\n"),
  };
}

function waitForWorker() {
  return new Promise((resolve, reject) => {
    const child = spawn(workerPath, ["--stdio", "--instance-id", instanceId], {
      cwd: repositoryRoot,
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    const events = [];
    let buffer = Buffer.alloc(0);
    let settled = false;
    let exitSeen = false;
    const timeout = setTimeout(() => finish(new Error("Terminal Worker bundle smoke test 超时")), 10000);

    const finish = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      if (error) {
        child.kill();
        reject(error);
      } else {
        resolve(events);
      }
    };

    child.on("error", (error) => finish(error));
    child.on("close", (code) => {
      if (!events.some((event) => event.type === "exit")) {
        finish(new Error(`Terminal Worker 提前退出，code=${code} events=${JSON.stringify(events)}`));
      } else {
        finish();
      }
    });
    child.stderr.on("data", () => {});
    child.stdout.on("data", (chunk) => {
      buffer = Buffer.concat([buffer, chunk]);
      while (buffer.length >= 4) {
        const size = buffer.readUInt32BE(0);
        if (buffer.length < size + 4) break;
        const event = JSON.parse(buffer.subarray(4, size + 4).toString("utf8"));
        events.push(event);
        buffer = buffer.subarray(size + 4);
        if (event.type === "handshake") {
          child.stdin.write(frame({ type: "write", input: { type: "raw", data_base64: shellSpec().input.toString("base64") } }));
        }
        if (event.type === "exit") exitSeen = true;
        if (exitSeen && events.some((candidate) => candidate.type === "output")) finish();
      }
    });

    const spec = shellSpec();
    child.stdin.write(frame({
      type: "start",
      shell: spec.shell,
      shell_kind: process.platform === "win32" ? "cmd" : path.basename(spec.shell[0]),
      cwd: repositoryRoot,
      // 只验证资源能启动、PTY 能收发；不注入 shell 集成脚本，避免冒烟结果依赖用户的 shell 配置。
      integration: "off",
    }));
  });
}

const events = await waitForWorker();
const output = Buffer.concat(
  events
    .filter((event) => event.type === "output")
    .map((event) => Buffer.from(event.data_base64, "base64")),
);
assert.match(output.toString("utf8"), /terminal-worker-bundle-smoke/);
assert.ok(events.some((event) => event.type === "handshake"));
assert.ok(events.some((event) => event.type === "exit"));
console.log(`Terminal Worker bundle smoke test passed：${workerPath}`);
