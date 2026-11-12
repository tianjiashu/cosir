import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import { reclaimIdleCacheDirs } from "@/scripts/dev-server-cache-dir-plugin";

const silentLogger = { info: () => {}, warn: () => {} };
const servers: net.Server[] = [];

afterEach(async () => {
  await Promise.all(servers.splice(0).map((server) => new Promise((resolve) => server.close(resolve))));
});

function listen(): Promise<number> {
  const server = net.createServer();
  servers.push(server);
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve((server.address() as net.AddressInfo).port)));
}

async function unusedPort(): Promise<number> {
  const port = await listen();
  await new Promise((resolve) => servers.pop()!.close(resolve));
  return port;
}

function cacheDir(root: string, port: number, ageDays: number): string {
  const directory = path.join(root, `port-${port}`);
  fs.mkdirSync(path.join(directory, "deps"), { recursive: true });
  const time = new Date(Date.now() - ageDays * 24 * 60 * 60 * 1000);
  fs.utimesSync(directory, time, time);
  return directory;
}

describe("reclaimIdleCacheDirs", () => {
  it("只回收闲置且端口无人监听的兄弟目录", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "cosir-vite-cache-"));
    const idle = cacheDir(root, await unusedPort(), 30);
    const listening = cacheDir(root, await listen(), 30);
    const recent = cacheDir(root, await unusedPort(), 1);
    const ownPort = await unusedPort();
    const own = cacheDir(root, ownPort, 30);
    fs.mkdirSync(path.join(root, "vitest"));

    const reclaimed = await reclaimIdleCacheDirs(root, ownPort, silentLogger);

    expect(reclaimed).toEqual([path.basename(idle)]);
    expect(fs.existsSync(idle)).toBe(false);
    for (const kept of [listening, recent, own, path.join(root, "vitest")]) expect(fs.existsSync(kept)).toBe(true);
  });
});
