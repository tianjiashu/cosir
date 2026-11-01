// 采集与对比一轮精简的可自动统计指标（标准 §7 中的 E5、E7、E9）。
//
// 用法：
//   node metrics.mjs snapshot --scope <scope.json> [--since <提交>] --out <快照.json>
//   node metrics.mjs compare <改动前快照.json> <改动后快照.json>
//
// snapshot 统计 scope.json（scope.mjs --json 的输出）中的文件；--since 把该提交以来新增或改动的文件
// 一并纳入，用于改动后快照覆盖本轮拆分出的新文件。
// 规模（E9）不依赖快照里的文件清单，直接取两次快照提交之间的 git numstat，所以本轮新建、删除的文件都计入。
// 复杂度（E7）依赖 lizard，重复率（E5）依赖 jscpd；工具不在 PATH 上时该项记为不可用，
// 由 Agent 按标准 §1 人工估算并标注「估算」。其余指标（E1–E4、E6、E8）需要判断，不在脚本范围内。
import { execFileSync, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// 仓库根由 git 从当前目录确定，与本 skill 安装在哪里无关。
const repositoryRoot = execFileSync("git", ["rev-parse", "--show-toplevel"], { encoding: "utf8" }).trim();

// 按路径约定识别测试文件，语言无关；生产与测试代码分开统计（标准 E9）。
const TEST_PATH = /(^|\/)(tests?|__tests__|spec)\/|\.(test|spec)\.[^/]+$|(^|\/)test_[^/]+\.py$|_test\.[^/]+$/;

// 关闭 core.quotePath：否则非 ASCII 路径会以转义加引号的形式输出，与磁盘上的真实路径对不上。
function git(...args) {
  return execFileSync("git", ["-c", "core.quotePath=false", ...args], { cwd: repositoryRoot, encoding: "utf8" }).trim();
}

function toolAvailable(name) {
  const probe = spawnSync(process.platform === "win32" ? "where" : "which", [name], { stdio: "ignore" });
  return probe.status === 0;
}

function parseCsvLine(line) {
  const cells = [];
  let cell = "";
  let quoted = false;
  for (const char of line) {
    if (char === '"') quoted = !quoted;
    else if (char === "," && !quoted) {
      cells.push(cell);
      cell = "";
    } else cell += char;
  }
  cells.push(cell);
  return cells;
}

// lizard --csv 列：NLOC, CCN, token, PARAM, length, location, file, function, long_name, start, end。
function complexity(files) {
  if (!toolAvailable("lizard")) return { available: false };
  const result = spawnSync("lizard", ["--csv", ...files], { cwd: repositoryRoot, encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
  if (result.error || result.status !== 0) return { available: false, error: result.stderr || String(result.error) };
  let max = null;
  for (const line of result.stdout.split("\n").filter(Boolean)) {
    const cells = parseCsvLine(line);
    const ccn = Number(cells[1]);
    if (Number.isFinite(ccn) && (!max || ccn > max.ccn)) max = { ccn, function: cells[8], file: cells[6] };
  }
  return { available: true, max };
}

function duplication(files) {
  if (!toolAvailable("jscpd")) return { available: false };
  const outputDir = mkdtempSync(join(tmpdir(), "code-simplification-jscpd-"));
  try {
    const result = spawnSync("jscpd", ["--silent", "--reporters", "json", "--output", outputDir, ...files], {
      cwd: repositoryRoot,
      encoding: "utf8",
    });
    const reportPath = join(outputDir, "jscpd-report.json");
    if (result.error || !existsSync(reportPath)) return { available: false, error: result.stderr || String(result.error) };
    const total = JSON.parse(readFileSync(reportPath, "utf8")).statistics.total;
    return { available: true, percentage: total.percentage, duplicatedLines: total.duplicatedLines };
  } finally {
    rmSync(outputDir, { recursive: true, force: true });
  }
}

function snapshot(args) {
  const scopePath = args[args.indexOf("--scope") + 1];
  const outPath = args[args.indexOf("--out") + 1];
  const sinceIndex = args.indexOf("--since");
  if (!args.includes("--scope") || !args.includes("--out")) throw new Error("snapshot 需要 --scope 与 --out");

  const files = new Set(JSON.parse(readFileSync(scopePath, "utf8")).files);
  if (sinceIndex !== -1) {
    for (const file of git("diff", "--name-only", "--diff-filter=d", "-M", `${args[sinceIndex + 1]}..HEAD`).split("\n")) {
      if (file) files.add(file);
    }
  }
  const existing = [...files].filter((file) => existsSync(join(repositoryRoot, file)));
  const production = existing.filter((file) => !TEST_PATH.test(file));

  // 有未提交改动时对工作区做快照（不改动工作区、暂存区与 stash 列表），前后两次快照都含同样的未提交改动，
  // 相减后只剩本轮改动；工作区干净时 stash create 输出为空，直接用 HEAD。
  const data = {
    commit: git("stash", "create") || git("rev-parse", "HEAD"),
    takenAt: new Date().toISOString(),
    files: existing,
    complexity: complexity(production),
    duplication: duplication(production),
  };
  writeFileSync(outPath, `${JSON.stringify(data, null, 2)}\n`);
  process.stdout.write(`快照已写入 ${outPath}（提交 ${data.commit.slice(0, 12)}，生产文件 ${production.length} 个）\n`);
}

function lineDelta(fromCommit, toCommit) {
  const totals = { production: { added: 0, deleted: 0 }, test: { added: 0, deleted: 0 } };
  for (const line of git("diff", "--numstat", "-M", `${fromCommit}..${toCommit}`).split("\n").filter(Boolean)) {
    const [added, deleted, path] = line.split("\t");
    if (added === "-") continue; // 二进制文件
    const bucket = TEST_PATH.test(path) ? totals.test : totals.production;
    bucket.added += Number(added);
    bucket.deleted += Number(deleted);
  }
  return totals;
}

function describe(metric, render) {
  return metric.available ? render(metric) : "不可用（需人工估算）";
}

function compare(args) {
  const [before, after] = args.map((path) => JSON.parse(readFileSync(path, "utf8")));
  const lines = lineDelta(before.commit, after.commit);
  const net = (bucket) => bucket.deleted - bucket.added;
  const ccn = (metric) => (metric.max ? `${metric.max.ccn}（${metric.max.function}）` : "无可统计函数");
  const dup = (metric) => `${metric.percentage}%`;

  const rows = [
    ["E5 重复率", describe(before.duplication, dup), describe(after.duplication, dup)],
    ["E7 最大圈复杂度", describe(before.complexity, ccn), describe(after.complexity, ccn)],
    ["E9 生产代码净删除", "—", `${net(lines.production)} 行（+${lines.production.added} / -${lines.production.deleted}）`],
    ["E9 测试代码变化", "—", `+${lines.test.added} / -${lines.test.deleted}`],
  ];
  process.stdout.write(`对比区间：${before.commit.slice(0, 12)} → ${after.commit.slice(0, 12)}\n\n`);
  process.stdout.write("| 指标 | 改动前 | 改动后 |\n|---|---|---|\n");
  for (const row of rows) process.stdout.write(`| ${row.join(" | ")} |\n`);
}

const [command, ...rest] = process.argv.slice(2);
if (command === "snapshot") snapshot(rest);
else if (command === "compare") compare(rest);
else {
  process.stderr.write("用法：metrics.mjs snapshot --scope <scope.json> [--since <提交>] --out <快照.json> | compare <前> <后>\n");
  process.exit(2);
}
