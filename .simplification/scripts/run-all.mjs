// 总入口：按 README.md 索引运行检测器或指标采集器，合并输出。
//
// 用法：node .simplification/scripts/run-all.mjs [--scope <scope.json>] [--metrics] [--json]
//   默认运行「检测器」，--metrics 只运行「采集器」（索引「类别」列）。
//   --json 时把合并结果写到 stdout（检测器为候选数组，采集器为 { metric, value, details } 数组），
//   进度写到 stderr，供 code-simplification skill 的 seeds.mjs、metrics.mjs 读取；否则在 stdout 打印可读摘要。
// 索引是唯一登记处：只运行 README.md 表格「文件」列登记的脚本。
// 单个脚本失败（非零退出且不是因为有发现）时记录并继续，最后以非零状态退出，避免失败被当成「没有发现」。
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const argv = process.argv.slice(2);
const scopePath = argv.includes("--scope") ? argv[argv.indexOf("--scope") + 1] : null;
const kind = argv.includes("--metrics") ? "采集器" : "检测器";
const json = argv.includes("--json");
const progress = json ? process.stderr : process.stdout;

// 索引表格前三列是「名称 | 文件 | 类别」，文件形如 `knip.mjs`。
const scripts = readFileSync(join(here, "README.md"), "utf8")
  .split("\n")
  .map((line) => line.match(/^\|\s*([^|]+?)\s*\|\s*`([\w.-]+\.mjs)`\s*\|\s*([^|]+?)\s*\|/))
  .filter((match) => match && match[3] === kind)
  .map(([, name, file]) => ({ name, file }));

const results = [];
const failures = [];
for (const { name, file } of scripts) {
  const started = Date.now();
  const result = spawnSync(process.execPath, [join(here, file), ...(scopePath ? ["--scope", scopePath] : [])], {
    encoding: "utf8",
    maxBuffer: 256 * 1024 * 1024,
  });
  const lines = result.stdout.split("\n").filter(Boolean);
  for (const line of lines) results.push(JSON.parse(line));
  const seconds = ((Date.now() - started) / 1000).toFixed(1);
  if (result.status !== 0) {
    failures.push(name);
    process.stderr.write(`[${name}] 失败（退出码 ${result.status}，${seconds}s）：\n${result.stderr.slice(-2000)}\n`);
  } else {
    progress.write(`[${name}] ${lines.length} 条（${seconds}s）\n`);
  }
}

if (json) {
  process.stdout.write(`${JSON.stringify(results, null, 2)}\n`);
} else if (kind === "采集器") {
  for (const { metric, value, details } of results) process.stdout.write(`${metric}: ${value}${details ? `（${details}）` : ""}\n`);
} else {
  for (const { transform, path, line, evidence } of results) {
    process.stdout.write(`${transform} ${path}${line ? `:${line}` : ""} ${evidence}\n`);
  }
  const counts = {};
  for (const { detector, transform } of results) counts[`${detector} ${transform}`] = (counts[`${detector} ${transform}`] ?? 0) + 1;
  process.stdout.write(`\n共 ${results.length} 个候选\n`);
  for (const [key, count] of Object.entries(counts).sort()) process.stdout.write(`  ${key}: ${count}\n`);
}
if (failures.length) {
  process.stderr.write(`\n失败的脚本：${failures.join("、")}\n`);
  process.exitCode = 1;
}
