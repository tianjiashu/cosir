import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const commands = [
  {
    name: "Knip",
    command: "npm",
    args: ["run", "knip"],
    cwd: resolve(repositoryRoot, "apps/desktop"),
  },
  {
    name: "Vulture",
    command: "uv",
    args: ["run", "--project", ".", "--group", "dev", "vulture"],
    cwd: resolve(repositoryRoot, "apps/backend"),
  },
];

let failed = false;

for (const { name, command, args, cwd } of commands) {
  process.stdout.write(`\n== ${name} ==\n`);
  const executable = process.platform === "win32" && command === "npm" ? "npm.cmd" : command;
  const result = spawnSync(executable, args, {
    cwd,
    stdio: "inherit",
    shell: process.platform === "win32" && command === "npm",
  });

  if (result.error) {
    process.stderr.write(`${name} could not start: ${result.error.message}\n`);
    failed = true;
  } else if (result.status !== 0) {
    failed = true;
  }
}

process.exitCode = failed ? 1 : 0;
