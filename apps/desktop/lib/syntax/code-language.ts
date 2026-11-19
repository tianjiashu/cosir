import { LanguageDescription } from "@codemirror/language";
import { languages } from "@codemirror/language-data";

import { baseName, fileExtension } from "@/lib/files/file-name";

/**
 * language-data 认不出、但内容其实是已知语言的后缀：Jupyter 笔记本就是 JSON。只做别名，不格式化——笔记本保存时
 * 本来就带缩进，格式化会让行号与引用、全文查找的位置对不上。
 */
const LANGUAGE_ALIASES: Record<string, string> = { ipynb: "JSON" };

/**
 * 按文件找语法高亮用的语言（文件阅读器与 diff 共用），给文件名或带目录的路径都行：只看文件名，因为
 * language-data 里 Dockerfile、CMakeLists.txt 这类按名字认的规则要求完整匹配（`/^Dockerfile$/`）。
 * 认不出返回 null，调用方按纯文本显示；语言包由调用方 `load()`。
 */
export function languageFor(path: string): LanguageDescription | null {
  const name = baseName(path);
  const alias = LANGUAGE_ALIASES[fileExtension(name)?.toLowerCase() ?? ""];
  return alias ? (LanguageDescription.matchLanguageName(languages, alias, false) ?? null) : LanguageDescription.matchFilename(languages, name);
}
