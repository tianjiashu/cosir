import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";

import { stopProcessTree } from "@/electron/main/backend/process";
import { readBootstateFailure } from "@/electron/main/backend/readiness";

function tempDirectory(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), "cosir-backend-process-"));
}

describe("bootstate failure", () => {
  it("reads the backend's structured failure", () => {
    const file = path.join(tempDirectory(), "backend.bootstate.json");
    fs.writeFileSync(file, JSON.stringify({ phase: "failed", error_type: "ImportError", error_message: "缺少依赖" }));
    expect(readBootstateFailure(file)).toBe("本地 Agent 后端启动失败（ImportError）：缺少依赖");
  });

  it("ignores a missing file or a non-failed phase", () => {
    const file = path.join(tempDirectory(), "backend.bootstate.json");
    expect(readBootstateFailure(file)).toBeNull();
    fs.writeFileSync(file, JSON.stringify({ phase: "booting" }));
    expect(readBootstateFailure(file)).toBeNull();
  });
});

describe("stop process tree", () => {
  // 目的：正常停止复用宿主存活契约（关闭 stdin → 后端读到 EOF 自行退出）；潜在缺陷：停止只靠信号，
  // Windows 无窗口进程收不到优雅退出请求，lifespan 收尾从不执行。
  it("asks the child to exit by closing its stdin", async () => {
    const child = spawn(process.execPath, ["-e", "process.stdin.on('end', () => process.exit(0)); process.stdin.resume();"], {
      stdio: ["pipe", "ignore", "ignore"],
    });
    const started = Date.now();

    await stopProcessTree(child);

    expect(child.exitCode).toBe(0);
    expect(Date.now() - started).toBeLessThan(5_000);
  });
});

describe("force-killing the process tree", () => {
  function isAlive(pid: number): boolean {
    try {
      process.kill(pid, 0);
      return true;
    } catch {
      return false;
    }
  }

  // 目的：后端不理会 stdin 关闭（卡在收尾里）时，宽限期后强杀整棵进程树，后端拉起的子进程一起结束。
  // 潜在缺陷：Windows 上只结束后端本身（少了 `/T`），或用不带 `/F` 的 taskkill 结束不了无窗口的控制台进程。
  it("kills the child and its descendants after the grace period", async () => {
    const grandchild = "setInterval(() => {}, 1000);";
    const script = `
      const { spawn } = require("node:child_process");
      const child = spawn(process.execPath, ["-e", ${JSON.stringify(grandchild)}], { stdio: "ignore" });
      process.stdout.write("GRANDCHILD " + child.pid + "\\n");
      process.stdin.resume();
      setInterval(() => {}, 1000);
    `;
    // 与 spawnBackend 一致：POSIX 上成为进程组长，强杀才能打到整组。
    const child = spawn(process.execPath, ["-e", script], {
      stdio: ["pipe", "pipe", "ignore"],
      detached: process.platform !== "win32",
      windowsHide: true,
    });
    const grandchildPid = await new Promise<number>((resolve) => {
      child.stdout!.on("data", (chunk: Buffer) => {
        const match = /GRANDCHILD (\d+)/.exec(chunk.toString());
        if (match) resolve(Number(match[1]));
      });
    });

    await stopProcessTree(child);

    expect(child.exitCode !== null || child.signalCode !== null).toBe(true);
    await expect.poll(() => isAlive(grandchildPid), { timeout: 5_000 }).toBe(false);
  }, 30_000);
});
