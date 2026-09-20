import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = path.resolve(desktopRoot, "..", "..");
const executableName = process.platform === "win32" ? "terminal-worker.exe" : "terminal-worker";
const instanceId = `bundle-smoke-${Date.now()}`;
const verifyPackaged = process.env.COSIR_TERMINAL_VERIFY_PACKAGED === "true";

const packagedResourceRoots = [
  process.env.COSIR_TERMINAL_RESOURCE_DIR,
  path.join(repositoryRoot, "target", "resources"),
  path.join(repositoryRoot, "target"),
  process.env.COSIR_TERMINAL_WORKER_TARGET &&
    path.join(
      repositoryRoot,
      "target",
      process.env.COSIR_TERMINAL_WORKER_TARGET,
      "debug",
    ),
  process.env.COSIR_TERMINAL_WORKER_TARGET &&
    path.join(
      repositoryRoot,
      "target",
      process.env.COSIR_TERMINAL_WORKER_TARGET,
      "release",
    ),
];
const candidateResourceRoots = (verifyPackaged
  ? packagedResourceRoots
  : [process.env.COSIR_TERMINAL_RESOURCE_DIR, path.join(repositoryRoot, "target", "resources")]
).filter(Boolean).map((candidate) => path.resolve(candidate));
const resourceRoot = findResourceRoot(candidateResourceRoots);
assert.ok(resourceRoot, `未找到 Tauri Terminal Worker resource，检查路径：${candidateResourceRoots.join(", ")}`);
const workerPath = path.join(resourceRoot, "terminal-worker", executableName);

function findResourceRoot(candidates) {
  for (const candidate of candidates) {
    const directPath = path.join(candidate, "terminal-worker", executableName);
    if (fs.existsSync(directPath)) return candidate;
    if (!verifyPackaged || !fs.existsSync(candidate)) continue;

    const pending = [{ directory: candidate, depth: 0 }];
    while (pending.length > 0) {
      const current = pending.shift();
      if (!current) continue;
      let entries;
      try {
        entries = fs.readdirSync(current.directory, { withFileTypes: true });
      } catch {
        continue;
      }
      for (const entry of entries) {
        if (!entry.isDirectory() || entry.isSymbolicLink()) continue;
        const directory = path.join(current.directory, entry.name);
        if (fs.existsSync(path.join(directory, "terminal-worker", executableName))) {
          return directory;
        }
        if (current.depth < 5) pending.push({ directory, depth: current.depth + 1 });
      }
    }
  }
  return undefined;
}

assert.equal(fs.existsSync(workerPath), true, `Terminal Worker resource 不存在：${workerPath}`);
if (process.platform !== "win32") {
  assert.equal(fs.statSync(workerPath).mode & 0o111, 0o111, "Terminal Worker 没有执行权限");
}

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
          child.stdin.write(frame({ type: "write", data_base64: shellSpec().input.toString("base64") }));
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
