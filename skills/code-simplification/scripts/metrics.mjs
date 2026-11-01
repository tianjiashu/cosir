// 采集与对比一轮精简的可自动统计指标（标准 §7），按 detection-scripts.md「指标三层」取值。
//
// 用法：
//   node metrics.mjs snapshot --scope <scope.json> [--candidates <候选.json>] [--collected <采集.json>]
//                             [--deep-at 6] --out <快照.json>
//   node metrics.mjs compare <改动前快照.json> <改动后快照.json>
//
// 输入都由 Agent 先跑好再传进来：本 skill 不调用项目的 run-all，它可能是任何语言写的。
//   --scope       scope.mjs --json 的输出；改动后快照用 scope.mjs --since 生成的范围，覆盖本轮新建的文件。
//   --candidates  项目 run-all --json --scope 的输出。E4 = M6 候选数，E8 = F1–F6 与 M9 候选数，
//                 透传层数 = M5 候选数（E3 的线索）。只数范围内生产文件上的候选，是检测器能发现的下界。
//   --collected   项目 run-all --metrics --json --scope 的输出（{ metric, value, details }），提供 E5–E7 等
//                 需要语言或项目工具才能算的整体指标。
//   --deep-at     缩进近似的「深层」阈值，默认 6 层。
// E7 没有项目采集器时，退回缩进近似（lib/indent.mjs），标「近似」；两者不同时出现，避免同一指标两个来源。
// 规模（E9）不依赖快照里的文件清单，直接取两次快照提交之间的 git numstat，所以本轮新建、删除的文件都计入。
// E1 的改动前证据在 scope.json 的 coChange 里；E1 改动后值、E2、E3 需要判断，不在脚本范围内。
import { readFileSync, writeFileSync } from "node:fs";

import { indentProfile } from "./lib/indent.mjs";
import { fail, git, isLockfile, isTest, readText } from "./lib/repo.mjs";

// 由候选按条款计数得出的指标；条款编号见标准 §4。
const COUNTED = [
  { key: "E4", label: "E4 单实现抽象（M6 候选，下界）", transforms: ["M6"] },
  { key: "E8", label: "E8 冗余点（F1–F6、M9 候选，下界）", transforms: ["F1", "F2", "F3", "F4", "F5", "F6", "M9"] },
  { key: "M5", label: "透传层（M5 候选，E3 线索）", transforms: ["M5"] },
];

function option(args, name) {
  const index = args.indexOf(name);
  return index === -1 ? null : args[index + 1];
}

function readJson(path) {
  const value = JSON.parse(readFileSync(path, "utf8"));
  if (!Array.isArray(value)) fail(`${path} 需要 JSON 数组`);
  return value;
}

function countCandidates(path, production) {
  if (!path) return null;
  const counts = {};
  for (const { transform, path: file } of readJson(path)) {
    if (production.has(file)) counts[transform] = (counts[transform] ?? 0) + 1;
  }
  return counts;
}

function indentApproximation(files, deepAt) {
  let max = null;
  let deep = 0;
  for (const file of files.filter((path) => !isLockfile(path))) {
    const text = readText(file);
    if (text === null) continue;
    const profile = indentProfile(text, deepAt);
    deep += profile.deep;
    if (profile.max && (!max || profile.max.level > max.level)) max = { ...profile.max, file };
  }
  return { deepAt, max, deep };
}

function snapshot(args) {
  const scopePath = option(args, "--scope");
  const outPath = option(args, "--out");
  if (!scopePath || !outPath) fail("snapshot 需要 --scope 与 --out");
  const deepAt = Number(option(args, "--deep-at") ?? 6);
  if (!Number.isInteger(deepAt) || deepAt <= 0) fail("--deep-at 需要一个正整数");

  const files = JSON.parse(readFileSync(scopePath, "utf8")).files;
  const production = files.filter((file) => !isTest(file) && readText(file) !== null);
  const collectedPath = option(args, "--collected");
  const collected = collectedPath ? Object.fromEntries(readJson(collectedPath).map((item) => [item.metric, item])) : null;

  // 有未提交改动时对工作区做快照（不改动工作区、暂存区与 stash 列表），前后两次快照都含同样的未提交改动，
  // 相减后只剩本轮改动；工作区干净时 stash create 输出为空，直接用 HEAD。
  const data = {
    commit: git("stash", "create") || git("rev-parse", "HEAD"),
    takenAt: new Date().toISOString(),
    files: production,
    counts: countCandidates(option(args, "--candidates"), new Set(production)),
    collected,
    indent: collected?.E7 ? null : indentApproximation(production, deepAt),
  };
  writeFileSync(outPath, `${JSON.stringify(data, null, 2)}\n`);
  process.stdout.write(`快照已写入 ${outPath}（提交 ${data.commit.slice(0, 12)}，生产文件 ${production.length} 个）\n`);
}

function lineDelta(fromCommit, toCommit) {
  const totals = { production: { added: 0, deleted: 0 }, test: { added: 0, deleted: 0 } };
  for (const line of git("diff", "--numstat", "-M", `${fromCommit}..${toCommit}`).split("\n").filter(Boolean)) {
    const [added, deleted, path] = line.split("\t");
    if (added === "-") continue; // 二进制文件
    const bucket = isTest(path) ? totals.test : totals.production;
    bucket.added += Number(added);
    bucket.deleted += Number(deleted);
  }
  return totals;
}

const UNAVAILABLE = "不可用（需人工估算）";

function countCell(shot, transforms) {
  if (!shot.counts) return "不可用（未给 --candidates）";
  return String(transforms.reduce((sum, transform) => sum + (shot.counts[transform] ?? 0), 0));
}

function collectedCell(shot, metric) {
  const item = shot.collected?.[metric];
  if (!item) return UNAVAILABLE;
  return item.details ? `${item.value}（${item.details}）` : String(item.value);
}

function indentCell(shot) {
  const { deepAt, max, deep } = shot.indent;
  if (!max) return "无可统计代码行";
  return `最深 ${max.level} 层（${max.file}:${max.line}），≥${deepAt} 层 ${deep} 行`;
}

function compare(args) {
  const [before, after] = args.map((path) => JSON.parse(readFileSync(path, "utf8")));
  const lines = lineDelta(before.commit, after.commit);
  const net = (bucket) => bucket.deleted - bucket.added;

  const rows = COUNTED.map(({ label, transforms }) => [label, countCell(before, transforms), countCell(after, transforms)]);
  rows.push(["E5 重复率（项目采集器）", collectedCell(before, "E5"), collectedCell(after, "E5")]);
  rows.push(["E6 依赖健康（项目采集器）", collectedCell(before, "E6"), collectedCell(after, "E6")]);
  if (before.indent && after.indent) rows.push(["E7 复杂度（缩进近似）", indentCell(before), indentCell(after)]);
  else rows.push(["E7 复杂度（项目采集器）", collectedCell(before, "E7"), collectedCell(after, "E7")]);
  rows.push(["E9 生产代码净删除", "—", `${net(lines.production)} 行（+${lines.production.added} / -${lines.production.deleted}）`]);
  rows.push(["E9 测试代码变化", "—", `+${lines.test.added} / -${lines.test.deleted}`]);

  process.stdout.write(`对比区间：${before.commit.slice(0, 12)} → ${after.commit.slice(0, 12)}\n\n`);
  process.stdout.write("| 指标 | 改动前 | 改动后 |\n|---|---|---|\n");
  for (const row of rows) process.stdout.write(`| ${row.join(" | ")} |\n`);
}

const [command, ...rest] = process.argv.slice(2);
if (command === "snapshot") snapshot(rest);
else if (command === "compare") compare(rest);
else fail("用法：metrics.mjs snapshot --scope <scope.json> [--candidates <文件>] [--collected <文件>] --out <快照.json> | compare <前> <后>");
