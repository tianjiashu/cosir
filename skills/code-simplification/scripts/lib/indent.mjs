// 缩进复杂度：语言无关的复杂度近似（Tornhill《Your Code as a Crime Scene》的做法）。
//
// 只看每行前导空白的层级，不解析语法，所以任何语言都能算。它是项目没有复杂度采集器时 E7 的退路，
// 也是 seeds.mjs 热点的复杂度因子；结论一律标「近似」，项目采集器给出 E7 时以项目为准（detection-scripts.md）。
// 已知误差：多行参数与续行缩进会抬高层级；类、命名空间的外层缩进也计入。纯格式改动会改变它，
// 但标准 §1 把格式化排除在精简之外，所以前后对比里不会出现这类噪声。

// 常见的行注释与块注释续行前缀；注释的对齐空格（如 JSDoc 的 ` * `）会把缩进单位推断成 1。
const COMMENT_LINE = /^(\/\/|\/\*|\*|#|--|;|%|<!--)/;

function leadingWidth(line) {
  let tabs = 0;
  let spaces = 0;
  for (const char of line) {
    if (char === "\t") tabs += 1;
    else if (char === " ") spaces += 1;
    else break;
  }
  return { tabs, spaces };
}

// 缩进单位取「相邻代码行缩进增量」的众数：比取最大公约数稳，个别对齐空格不会把单位拉成 1。
function inferUnit(widths) {
  const increases = new Map();
  for (let i = 1; i < widths.length; i += 1) {
    const delta = widths[i] - widths[i - 1];
    if (delta > 0) increases.set(delta, (increases.get(delta) ?? 0) + 1);
  }
  let unit = 4;
  let best = 0;
  for (const [delta, count] of increases) {
    if (count > best || (count === best && delta < unit)) {
      unit = delta;
      best = count;
    }
  }
  return unit;
}

// 返回 { total, max, deep }：total 是全部代码行层级之和，max 是层级最深的一行（{ level, line }，1 起算行号；
// 没有代码行时为 null），deep 是层级不低于 deepAt 的行数。
export function indentProfile(text, deepAt = Infinity) {
  const rows = [];
  text.split("\n").forEach((raw, index) => {
    const trimmed = raw.trim();
    if (!trimmed || COMMENT_LINE.test(trimmed)) return;
    rows.push({ ...leadingWidth(raw), line: index + 1 });
  });
  const unit = inferUnit(rows.map((row) => row.spaces));
  let total = 0;
  let max = null;
  let deep = 0;
  for (const row of rows) {
    const level = row.tabs + Math.floor(row.spaces / unit);
    total += level;
    if (level >= deepAt) deep += 1;
    if (!max || level > max.level) max = { level, line: row.line };
  }
  return { total, max, deep };
}
