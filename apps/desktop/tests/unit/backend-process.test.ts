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
