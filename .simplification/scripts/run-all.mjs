// 总入口：按 README.md 索引运行全部检测器，合并候选。
//
// 用法：node .simplification/scripts/run-all.mjs [--scope <scope.json>] [--out <候选.json>]
// 索引是唯一登记处：只运行 README.md 表格「文件」列登记的脚本。
// 单个检测器失败（非零退出且不是因为有发现）时记录并继续，最后以非零状态退出，避免失败被当成「没有发现」。
import { spawnSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const argv = process.argv.slice(2);
const option = (name) => (argv.includes(name) ? argv[argv.indexOf(name) + 1] : null);
const scopePath = option("--scope");
const outPath = option("--out") ?? join(tmpdir(), `simplification-candidates-${Date.now()}.json`);

// 索引表格的第二列是「文件」，形如 `knip.mjs`。
const detectors = readFileSync(join(here, "README.md"), "utf8")
  .split("\n")
  .map((line) => line.match(/^\|\s*([^|]+?)\s*\|\s*`([\w.-]+\.mjs)`\s*\|/))
  .filter(Boolean)
  .map(([, name, file]) => ({ name, file }));

const candidates = [];
const failures = [];
for (const { name, file } of detectors) {
  const started = Date.now();
  const result = spawnSync(process.execPath, [join(here, file), ...(scopePath ? ["--scope", scopePath] : [])], {
    encoding: "utf8",
    maxBuffer: 256 * 1024 * 1024,
  });
  const lines = result.stdout.split("\n").filter(Boolean);
  for (const line of lines) candidates.push(JSON.parse(line));
  const seconds = ((Date.now() - started) / 1000).toFixed(1);
  if (result.status !== 0) {
    failures.push(name);
    process.stderr.write(`[${name}] 失败（退出码 ${result.status}，${seconds}s）：\n${result.stderr.slice(-2000)}\n`);
  } else {
    process.stdout.write(`[${name}] ${lines.length} 个候选（${seconds}s）\n`);
  }
}

writeFileSync(outPath, `${JSON.stringify(candidates, null, 2)}\n`);

const counts = {};
for (const { detector, transform } of candidates) counts[`${detector} ${transform}`] = (counts[`${detector} ${transform}`] ?? 0) + 1;
process.stdout.write(`\n共 ${candidates.length} 个候选，已写入 ${outPath}\n`);
for (const [key, count] of Object.entries(counts).sort()) process.stdout.write(`  ${key}: ${count}\n`);
if (failures.length) {
  process.stderr.write(`\n失败的检测器：${failures.join("、")}\n`);
  process.exitCode = 1;
}
