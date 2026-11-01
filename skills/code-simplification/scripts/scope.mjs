// 计算一轮精简的默认范围：上一轮基准提交以来变更过、且仍存在的文件。
//
// 用法：node <skill>/scripts/scope.mjs [--base <提交>] [--json]
//   在目标项目的仓库内任意位置运行；仓库根由 git 确定，与本 skill 安装在哪里无关。
//   --base  上一轮报告中的「本轮基准」；省略时从最新一份报告里读取，读不到则报错退出，
//           由 Agent 按标准 §6.2 请人类指定范围（首轮不得自行全仓扫描）。
//   --json  输出机器可读结果，供 metrics.mjs 的 --scope 使用。
//
// 只给出「变更过的文件」；直接调用方与被调用方依赖语言级分析，由 Agent 补齐。
import { execFileSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

const repositoryRoot = execFileSync("git", ["rev-parse", "--show-toplevel"], { encoding: "utf8" }).trim();
// 精简状态目录固定在仓库根的 .simplification/（SKILL.md「目标项目状态」）。
const reportsDir = join(repositoryRoot, ".simplification/reports");

// 关闭 core.quotePath：否则非 ASCII 路径会以转义加引号的形式输出，与磁盘上的真实路径对不上。
function git(...args) {
  return execFileSync("git", ["-c", "core.quotePath=false", ...args], { cwd: repositoryRoot, encoding: "utf8" }).trim();
}

function parseArgs(argv) {
  const options = { base: null, json: false };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--base") options.base = argv[++i];
    else if (argv[i] === "--json") options.json = true;
    else throw new Error(`未知参数：${argv[i]}`);
  }
  return options;
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
const base = options.base ?? fromReport?.commit;
if (!base) {
  process.stderr.write("找不到上一轮基准：请用 --base 指定，或按标准 §6.2 请人类指定首轮范围。\n");
  process.exit(2);
}

const head = git("rev-parse", "HEAD");
// --diff-filter=d 排除已删除的文件；重命名按新路径计入。
const files = git("diff", "--name-only", "--diff-filter=d", "-M", `${base}..HEAD`)
  .split("\n")
  .filter(Boolean);
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
  process.stdout.write(`${JSON.stringify({ base, head, files, dirtyFiles }, null, 2)}\n`);
} else {
  process.stdout.write(`上一轮基准：${base}${fromReport ? `（来自 ${fromReport.report}）` : ""}\n`);
  process.stdout.write(`本轮基准（HEAD）：${head}\n`);
  process.stdout.write(`变更文件 ${files.length} 个：\n`);
  for (const file of files) process.stdout.write(`  ${file}\n`);
  if (dirtyFiles.length) {
    process.stdout.write(`有未提交改动的文件 ${dirtyFiles.length} 个（冻结快照方式下本轮只读）：\n`);
    for (const file of dirtyFiles) process.stdout.write(`  ${file}\n`);
  }
}
