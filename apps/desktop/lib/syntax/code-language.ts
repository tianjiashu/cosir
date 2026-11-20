import { LanguageDescription } from "@codemirror/language";
import { languages } from "@codemirror/language-data";
import { bundledLanguagesInfo } from "shiki/langs";

import { baseName, fileExtension } from "@/lib/files/file-name";

/**
 * language-data 认不出、但内容其实是已知语言的后缀：Jupyter 笔记本就是 JSON。只做别名，不格式化——笔记本保存时
 * 本来就带缩进，格式化会让行号与引用、全文查找的位置对不上。
 */
const LANGUAGE_ALIASES: Record<string, string> = { ipynb: "JSON" };

/**
 * 按文件认语言（全仓唯一的识别入口），给文件名或带目录的路径都行：只看文件名，因为 language-data 里
 * Dockerfile、CMakeLists.txt 这类按名字认的规则要求完整匹配（`/^Dockerfile$/`）。认不出返回 null。
 * 文件阅读器用它加载 CodeMirror 的结构语言包（由调用方 `load()`）；着色用的 Shiki 名经 `highlightLanguageOf` 换算。
 */
export function languageFor(path: string): LanguageDescription | null {
  const name = baseName(path);
  const alias = LANGUAGE_ALIASES[fileExtension(name)?.toLowerCase() ?? ""];
  return alias ? (LanguageDescription.matchLanguageName(languages, alias, false) ?? null) : LanguageDescription.matchFilename(languages, name);
}

/**
 * Shiki 的语言名索引：语言 id、显示名与别名（小写）→ 语言 id。Markdown 代码块的围栏语言（`ts`、`shell`）与
 * 下面按文件换算的名字都在这里查。
 */
const SHIKI_LANGUAGES = new Map<string, string>(
  bundledLanguagesInfo.flatMap((info) => [info.id, info.name, ...(info.aliases ?? [])].map((key) => [key.toLowerCase(), info.id] as const)),
);

/**
 * language-data 的语言名与 Shiki 对不上、但 Shiki 有对应语法的：SQL 各方言、Angular 模板等。只放别名，
 * 不为个别语言写着色规则；对不上又不在这里的语言按原文显示（CodeMirror 仍负责它的折叠、缩进等结构）。
 */
const SHIKI_ALIASES: Record<string, string> = {
  "mariadb sql": "sql", "ms sql": "sql", mysql: "sql", postgresql: "sql", sqlite: "sql",
  "angular template": "angular-html", "json-ld": "json", clojurescript: "clojure", cython: "python",
  // language-data 的 CQL 是 Cassandra 的查询语言（.cql），Shiki 把 `cql` 当作 Cypher 的别名。
  cql: "sql",
};

/**
 * language-data 把几种方言认成同一个语言、Shiki 按后缀分开的：Fortran 的自由格式与固定格式。只列确实不同的后缀，
 * 不按后缀另起一套识别（`.v` 是 Verilog——language-data 认作 SystemVerilog——不是 Shiki 的 V 语言，必须以 `languageFor` 为准）。
 */
const SHIKI_DIALECTS: Record<string, string> = { f90: "fortran-free-form", f95: "fortran-free-form" };

/** Markdown 代码块的围栏语言 → Shiki 语言名；不认识时 null。 */
export function highlightLanguageOfFence(fence: string): string | null {
  return SHIKI_LANGUAGES.get(fence.trim().toLowerCase()) ?? null;
}

/**
 * 文件 → Shiki 语言名（着色用）。「这是什么语言」只由 `languageFor` 判断（文件名规则与后缀），这里只把它的结果
 * 换算成 Shiki 的名字，不另起一套识别。
 */
export function highlightLanguageOf(path: string): string | null {
  const description = languageFor(path);
  if (!description) return null;
  const dialect = SHIKI_DIALECTS[fileExtension(baseName(path))?.toLowerCase() ?? ""];
  if (dialect) return dialect;
  const keys = [description.name, ...description.alias, ...description.extensions].map((key) => key.toLowerCase());
  for (const key of keys) {
    const found = SHIKI_ALIASES[key] ?? SHIKI_LANGUAGES.get(key);
    if (found) return found;
  }
  return null;
}
