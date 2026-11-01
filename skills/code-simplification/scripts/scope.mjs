// 计算一轮精简的范围：种子的文件集（按路径取），或上一轮基准以来变更过、且仍存在的文件。
//
// 用法：node <skill>/scripts/scope.mjs [--seed <路径>]... [--base <提交>] [--json]
//   在目标项目的仓库内任意位置运行；仓库根由 git 确定，与本 skill 安装在哪里无关。
//   --seed    一个文件或目录，可重复。给出时范围就是这些路径下的跟踪文件，对应标准 §6.2 的
//             种子与扩散：脚本只做路径这一层，依赖边（调用方、数据流、同一事实源）由 Agent 补齐。
//   --base    上一轮报告中的「本轮基准」；省略时从最新一份报告里读取。用于复检模式（只读快扫）。
//   --json    输出机器可读结果，供 metrics.mjs 的 --scope 使用。
//
// 不给 --seed 时按提交取变更文件：那是复检模式，产出候选供选种，不在本轮直接修改（标准 §6.2）。
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { fail, git, repositoryRoot } from "./lib/repo.mjs";

// 精简状态目录固定在仓库根的 .simplification/（SKILL.md「目标项目状态」）。
const reportsDir = join(repositoryRoot, ".simplification", "reports");

function parseArgs(argv) {
  const options = { base: null, json: false, seeds: [] };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--base") options.base = argv[++i];
    else if (argv[i] === "--seed") options.seeds.push(argv[++i]);
    else if (argv[i] === "--json") options.json = true;
    else fail(`未知参数：${argv[i]}`);
  }
  return options;
}

function underPrefixes(path, prefixes) {
  return prefixes.some((prefix) => {
    const base = prefix.endsWith("/") ? prefix.slice(0, -1) : prefix;
    return base === "." || path === base || path.startsWith(`${base}/`);
  });
}

// 种子按路径取文件：文件给自身，目录给其下的全部跟踪文件。
function seedFiles(seeds) {
  const tracked = git("ls-files").split("\n").filter(Boolean);
  const files = tracked.filter((file) => underPrefixes(file, seeds));
  if (files.length === 0) fail(`种子没有命中任何跟踪文件：${seeds.join("、")}`);
  return files;
}

// 报告名为 YYYY-MM-DD[-N].md，去掉扩展名后按字典序即时间序（带扩展名排序会把 -2 排到当天首份之前）；
// 基准写在「本轮基准：<提交>」一行（标准 §9 模板）。
function baseFromLatestReport() {
  if (!existsSync(reportsDir)) return null;
  const reports = readdirSync(reportsDir)
    .filter((name) => name.endsWith(".md"))
    .map((name) => name.slice(0, -3))
    .sort()
    .map((stem) => `${stem}.md`);
  const latest = reports.at(-1);
  if (!latest) return null;
  const match = readFileSync(join(reportsDir, latest), "utf8").match(/本轮基准[：:]\s*`?([0-9a-f]{7,40})`?/);
  return match ? { commit: match[1], report: latest } : null;
}

const options = parseArgs(process.argv.slice(2));

const fromReport = options.base ? null : baseFromLatestReport();
const base = options.base ?? fromReport?.commit ?? null;
const head = git("rev-parse", "HEAD");

let files;
if (options.seeds.length > 0) {
  files = seedFiles(options.seeds);
} else {
  if (!base) {
    fail("找不到上一轮基准：请用 --base 指定，或用 --seed 给出本轮种子（标准 §6.2）。");
  }
  // --diff-filter=d 排除已删除的文件；重命名按新路径计入。
  files = git("diff", "--name-only", "--diff-filter=d", "-M", `${base}..HEAD`)
    .split("\n")
    .filter(Boolean);
}

// 有未提交改动的文件在冻结快照方式下本轮只读（SKILL.md「执行方式」），这里列出供 Agent 排除。
// 用 -z 输出：路径不转义（非 ASCII 路径在默认格式下会被加引号转义）；每条为「两位状态码 + 空格 + 路径」，
// 重命名与复制条目后面还跟一条原路径，跳过。
const dirtyFiles = [];
const statusEntries = git("status", "--porcelain", "-z").split("\0").filter(Boolean);
for (let i = 0; i < statusEntries.length; i += 1) {
  const entry = statusEntries[i];
  dirtyFiles.push(entry.slice(3));
  if (/^[RC]|^.[RC]/.test(entry)) i += 1;
}

if (options.json) {
  const payload = { seeds: options.seeds, base, head, files, dirtyFiles };
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
} else {
  if (options.seeds.length > 0) process.stdout.write(`种子：${options.seeds.join("、")}\n`);
  process.stdout.write(`上一轮基准：${base ?? "（未使用）"}${fromReport ? `（来自 ${fromReport.report}）` : ""}\n`);
  process.stdout.write(`本轮基准（HEAD）：${head}\n`);
  process.stdout.write(`范围文件 ${files.length} 个：\n`);
  for (const file of files) process.stdout.write(`  ${file}\n`);
  if (dirtyFiles.length) {
    process.stdout.write(`有未提交改动的文件 ${dirtyFiles.length} 个（冻结快照方式下本轮只读）：\n`);
    for (const file of dirtyFiles) process.stdout.write(`  ${file}\n`);
  }
}
