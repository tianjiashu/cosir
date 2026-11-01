// 给标准 §6.2 的「选种子」排序：把一个尽量笨的分组（路径前缀）算成可比数值。
//
// 用法：node <skill>/scripts/seeds.mjs [--candidates <文件>] [--depth 2] [--days 90] [--top 12] [--json]
//   --candidates  总入口 run-all 的机读输出（JSON 数组或每行一个对象，每项含 path，
//                 见 references/detection-scripts.md「统一输出」）。不给时只统计变更热度与代码量。
//   --depth       分组用的路径段数，默认 2（如 app/tools）。它只是粗排的分组手段，
//                 **不是能力定义**：范围由种子扩散决定（标准 §6.2），不靠这份分组。
//   --days        变更热度的回看天数，默认 90。
//   --top         只打印前 N 组，默认 12；全部结果用 --json 取。
//
// 排序按候选密度（候选数 ÷ 生产代码行数），热度与行数只作参考列。分数只用来挑前几个种子，
// 不是结论：种子是文件或目录，不是「某个能力」。
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { fail, git, isTest, repositoryRoot } from "./lib/repo.mjs";

function parseArgs(argv) {
  const options = { candidates: null, depth: 2, days: 90, top: 12, json: false };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--candidates") options.candidates = argv[++i];
    else if (argv[i] === "--depth") options.depth = Number(argv[++i]);
    else if (argv[i] === "--days") options.days = Number(argv[++i]);
    else if (argv[i] === "--top") options.top = Number(argv[++i]);
    else if (argv[i] === "--json") options.json = true;
    else fail(`未知参数：${argv[i]}`);
  }
  for (const key of ["depth", "days", "top"]) {
    if (!Number.isInteger(options[key]) || options[key] <= 0) fail(`--${key} 需要一个正整数`);
  }
  return options;
}

// 分组键：取路径的前 depth 段；文件在更浅的层级时用它的全部目录段。
function groupOf(path, depth) {
  const segments = path.split("/");
  const directory = segments.slice(0, -1);
  if (directory.length === 0) return "(仓库根)";
  return directory.slice(0, depth).join("/");
}

function countLines(text) {
  if (text.length === 0) return 0;
  const newlines = (text.match(/\n/g) ?? []).length;
  return text.endsWith("\n") ? newlines : newlines + 1;
}

function productionLines(files) {
  let total = 0;
  for (const file of files) {
    if (isTest(file)) continue;
    try {
      const buffer = readFileSync(join(repositoryRoot, file));
      if (buffer.includes(0)) continue; // 二进制
      total += countLines(buffer.toString("utf8"));
    } catch {
      // 文件在列表里但读不到：跳过，代码量只作分母。
    }
  }
  return total;
}

// 候选位置可能是「文件:行」或纯符号；只取仓库相对路径部分。
function candidatePath(item) {
  const raw = String(item.path ?? item.location ?? "").trim();
  if (!raw || !raw.includes("/")) return null;
  return raw.replace(/:\d+(-\d+)?$/, "");
}

function readCandidates(path) {
  const raw = readFileSync(path, "utf8").trim();
  if (!raw) return [];
  const items = raw.startsWith("[")
    ? JSON.parse(raw)
    : raw.split("\n").filter(Boolean).map((line) => JSON.parse(line));
  if (!Array.isArray(items)) fail("--candidates 需要 JSON 数组或每行一个对象");
  return items;
}

// 近 N 天每个分组的变更热度与共动度。
// 热度：该分组被多少个提交动过（同一提交动多个分组时各记一次）。
// 共动：动到该分组的那些提交里，平均还动了几个别的分组——「一次需求牵动几个目录」的代理指标（E1）。
function commitsByGroup(depth, days) {
  const commits = new Map();
  const coChangeSum = new Map();
  const log = git("log", `--since=${days} days ago`, "--name-only", "--pretty=format:__commit__");
  for (const block of log.split("__commit__")) {
    const groups = new Set(
      block
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean)
        .map((path) => groupOf(path, depth)),
    );
    for (const group of groups) {
      commits.set(group, (commits.get(group) ?? 0) + 1);
      coChangeSum.set(group, (coChangeSum.get(group) ?? 0) + (groups.size - 1));
    }
  }
  return { commits, coChangeSum };
}

const options = parseArgs(process.argv.slice(2));
const tracked = git("ls-files").split("\n").filter(Boolean);
const candidates = options.candidates ? readCandidates(options.candidates) : null;
const { commits: heat, coChangeSum } = commitsByGroup(options.depth, options.days);

const filesByGroup = new Map();
for (const file of tracked) {
  const group = groupOf(file, options.depth);
  const bucket = filesByGroup.get(group) ?? [];
  bucket.push(file);
  filesByGroup.set(group, bucket);
}

const foundByGroup = new Map();
let unattributed = 0;
let testCandidates = 0;
for (const item of candidates ?? []) {
  const path = candidatePath(item);
  if (path === null) {
    unattributed += 1;
    continue;
  }
  // 分母只算生产代码，分子也只算生产代码里的候选，否则测试多的目录会被算得虚高。
  if (isTest(path)) {
    testCandidates += 1;
    continue;
  }
  const group = groupOf(path, options.depth);
  foundByGroup.set(group, (foundByGroup.get(group) ?? 0) + 1);
}

const rows = [...filesByGroup.entries()].map(([group, files]) => {
  const lines = productionLines(files);
  const found = candidates ? (foundByGroup.get(group) ?? 0) : null;
  const commits = heat.get(group) ?? 0;
  return {
    group,
    candidates: found,
    files: files.length,
    productionLines: lines,
    density: found === null || lines === 0 ? null : (found / lines) * 1000,
    commits,
    coChange: commits === 0 ? null : (coChangeSum.get(group) ?? 0) / commits,
  };
});

rows.sort((a, b) => {
  if (a.density !== b.density) {
    if (a.density === null) return 1;
    if (b.density === null) return -1;
    return b.density - a.density;
  }
  return b.commits - a.commits;
});

if (unattributed > 0) {
  process.stderr.write(`注意：${unattributed} 个候选没有可用的路径，未参与排序。\n`);
}
if (testCandidates > 0) {
  process.stderr.write(
    `注意：${testCandidates} 个候选落在测试文件里，未计入密度（分母只算生产代码）。\n`,
  );
}

const shown = rows.slice(0, options.top);

if (options.json) {
  const payload = { depth: options.depth, days: options.days, total: rows.length, groups: shown };
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
} else {
  process.stdout.write(
    `| 分组（前 ${options.depth} 段路径） | 候选数 | 文件数 | 生产行数 | 候选密度（每千行） | 近 ${options.days} 天提交 | 共动分组（平均） |\n`,
  );
  process.stdout.write("|---|---|---|---|---|---|---|\n");
  for (const row of shown) {
    const cells = [
      row.group,
      row.candidates === null ? "—" : String(row.candidates),
      String(row.files),
      String(row.productionLines),
      row.density === null ? "—" : row.density.toFixed(2),
      String(row.commits),
      row.coChange === null ? "—" : row.coChange.toFixed(1),
    ];
    process.stdout.write(`| ${cells.join(" | ")} |\n`);
  }
  process.stdout.write(
    `\n共 ${rows.length} 个分组，只列前 ${shown.length} 个。分组只是粗排手段，不是能力定义：` +
      "从表里挑一个具体的文件或目录作种子，范围由它扩散得出（标准 §6.2）。\n",
  );
}
