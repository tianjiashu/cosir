import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import { datedLogPath, MAX_LOG_BYTES, rotateIfNeeded, shardPath } from "@/electron/main/desktop-log";

const directories: string[] = [];

function tempDirectory(): string {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "cosir-desktop-log-"));
  directories.push(directory);
  return directory;
}

afterEach(() => {
  for (const directory of directories.splice(0)) fs.rmSync(directory, { recursive: true, force: true });
});

describe("desktop log files", () => {
  it("dated path keeps the name and extension", () => {
    expect(datedLogPath(path.join("logs", "backend-console.log"), "2026-09-13"))
      .toBe(path.join("logs", "backend-console-2026-09-13.log"));
  });

  // 目的：轮转在「写入这一行会超限」时发生，保证每个分片都不超过上限；潜在缺陷：先写后转会产出超限文件。
  it("rotates before the incoming line would exceed the limit", () => {
    const active = path.join(tempDirectory(), "frontend-2026-09-13.log");
    fs.writeFileSync(active, Buffer.alloc(MAX_LOG_BYTES - 1, "x"));

    rotateIfNeeded(active, 2);

    expect(fs.existsSync(active)).toBe(false);
    expect(fs.statSync(shardPath(active, 1)).size).toBe(MAX_LOG_BYTES - 1);
  });

  it("does not rotate an empty or small file", () => {
    const active = path.join(tempDirectory(), "frontend-2026-09-13.log");
    fs.writeFileSync(active, "line\n");

    rotateIfNeeded(active, 10);

    expect(fs.existsSync(active)).toBe(true);
    expect(fs.existsSync(shardPath(active, 1))).toBe(false);
  });
});
