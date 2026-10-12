#!/usr/bin/env python3
"""wiki 文档集的机械校验脚本（轻量辅助工具）。

定位：
    对 ``wiki/`` 下的 Markdown 文档做一轮**廉价的机械体检**，给撰写者和复核者提供客观信号。
    它是辅助工具，不是 CI 门禁：默认只有「客观事实错误」会让退出码非零，其余一律只提示。

严重程度模型（本脚本最重要的设计决策）：
    只有**不可能对合法文档判断错误**的规则才算 ``error``：

        FENCE_UNBALANCED  代码围栏未闭合
        LINK_MISSING      相对链接目标不存在
        POINTER_MISSING   反引号路径指针不存在
        BASELINE_INVALID  baseline_commit 不是 sha 形态

    凡是**可能被合法写法合理违反**的规则一律为 ``warning``：模板小节、mermaid、代码块长度、
    是否提及 ``docs/``、符号存在性、行号范围、基线是否标注。理由：这些规则用启发式判断，
    误报无法根除；而误报会让使用者（尤其是 Agent）学会忽略脚本输出，比不检查更糟。

已知盲区（有意不检，避免误报）：
    - 4 空格缩进的代码块不识别。Markdown 里缩进也可能来自列表嵌套，强行识别会误伤。
    - 图片语法 ``![alt](path)`` 不校验，图片常指向外部资源。
    - 类作用域不还原，符号核对只到「文件内是否出现过这个定义名」这一层。

为什么只用标准库：
    脚本要在开发者机器与后端进程侧直接运行，不应为它引入需要安装的第三方依赖（后端依赖
    由 uv 锁定，工具脚本不得污染该锁文件）。

退出码：
    0：仅有 warning（或完全无问题）。
    1：存在 error。
    2：用法或配置错误（``--wiki-dir`` 不存在、参数非法）。
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ERROR = "error"
WARNING = "warning"

SEVERITY_ORDER = {ERROR: 0, WARNING: 1}

# 规则严重程度的单一事实源：Issue 的级别由此表决定，避免在检查函数里散落级别常量。
# ``--list-rules`` 直接输出本表，供 ``wiki/CONVENTIONS.md`` 与之一一对应。
RULE_SEVERITY: dict[str, str] = {
    "FENCE_UNBALANCED": ERROR,
    "LINK_MISSING": ERROR,
    "POINTER_MISSING": ERROR,
    "BASELINE_INVALID": ERROR,
    "PAGES_REQUIRED": ERROR,
    "CODE_BLOCK_LONG": WARNING,
    "SECTION_MISSING": WARNING,
    "MERMAID_MISSING": WARNING,
    "DOCS_REFERENCE": WARNING,
    "SYMBOL_MISSING": WARNING,
    "LINE_REF_OUT_OF_RANGE": WARNING,
    "BASELINE_MISSING": WARNING,
    "BASELINE_UNKNOWN": WARNING,
    "CHECK_SKIPPED": WARNING,
    "GIT_UNAVAILABLE": WARNING,
    "NO_PAGES": WARNING,
}

RULE_DOC: dict[str, str] = {
    "FENCE_UNBALANCED": "代码围栏（```）未闭合",
    "LINK_MISSING": "相对链接目标文件不存在",
    "POINTER_MISSING": "反引号内的路径指针指向不存在的文件",
    "BASELINE_INVALID": "front-matter baseline_commit 不是合法 sha 形态",
    "PAGES_REQUIRED": "文档数为 0（终检要求至少一篇）",
    "CODE_BLOCK_LONG": "非 mermaid 代码块超过行数阈值（建议改为路径指针）",
    "SECTION_MISSING": "缺少模板要求的小节标题",
    "MERMAID_MISSING": "链路类文档（type: flow / 文件名含「链路」）没有 mermaid 图",
    "DOCS_REFERENCE": "文中出现 docs/ 字样（该目录为已过时历史计划）",
    "SYMBOL_MISSING": "路径:符号 中的符号未在源码中出现",
    "LINE_REF_OUT_OF_RANGE": "路径:行号 超出文件实际行数",
    "BASELINE_MISSING": "缺少 front-matter baseline_commit",
    "BASELINE_UNKNOWN": "baseline_commit 不在当前 git 仓库中",
    "CHECK_SKIPPED": "目标文件无法解析，该项检查被跳过",
    "GIT_UNAVAILABLE": "未找到 git，baseline 存在性未校验",
    "NO_PAGES": "未发现任何 Markdown 文档",
}

# 模板小节：按「标题前缀」匹配。用前缀而非包含，避免「## 定位与契约」一个标题同时顶掉
# 「定位」和「上下游契约」两个小节；同时给出同义前缀，降低合法变体被误报的概率。
SECTION_PREFIXES: dict[str, tuple[str, ...]] = {
    "定位": ("定位",),
    "关键文件": ("关键文件",),
    "上下游契约": ("上下游", "契约"),
    "不变量与坑": ("不变量", "注意事项", "约束"),
    "相关文档": ("相关文档", "相关阅读", "延伸阅读"),
}

# 豁免模板与 mermaid 要求的页面：索引页与规范页本身不以内容页格式书写。
EXEMPT_FILENAMES = frozenset({"README.md", "CONVENTIONS.md"})

# 已过时的历史计划目录；只提示，不阻断。
DOCS_REFERENCE_RE = re.compile(r"(?<![\w/-])docs/")

SOURCE_SUFFIXES = (
    ".py", ".md", ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg",
    ".txt", ".sh", ".ps1", ".cmd", ".ts", ".tsx", ".js", ".rs",
    ".sqlite3", ".mdc", ".svg",
)

ROOT_FILE_ALLOWLIST = frozenset({"AGENTS.md", "README.md", "Cargo.toml", "Cargo.lock"})

BACKTICK_RE = re.compile(r"`([^`\n]+)`")
LINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)\s]+)\)")
# 允许 ```lang 与 ```python title=x / ```ts {1,3}；缩进 4 空格以上不视为围栏（那是缩进代码块）。
FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*(.*)$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
NUMBERING_RE = re.compile(r"^(?:\d+[.、)]|[一二三四五六七八九十]+[、.)])\s*")
LINE_REF_RE = re.compile(r"^(\d+)(?:\s*-\s*(\d+))?$")
BASELINE_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
FRONT_MATTER_KEY_RE = re.compile(r"^[A-Za-z_][\w-]*\s*:")
EXTERNAL_TARGET_PREFIXES = ("http://", "https://", "mailto:", "tel:", "data:")

DEFAULT_MAX_CODE_BLOCK_LINES = 15


@dataclass(frozen=True)
class Issue:
    """一条校验结果。``line`` 为 None 表示问题不落在具体行。"""

    code: str
    file: str
    line: int | None
    message: str

    @property
    def severity(self) -> str:
        return RULE_SEVERITY.get(self.code, WARNING)


@dataclass
class Block:
    """一段代码围栏。``lang`` 为 info 串首个词，可能为空串。"""

    start: int
    end: int
    lang: str
    lines: list[str]


def severity_of(code: str) -> str:
    return RULE_SEVERITY.get(code, WARNING)


def split_front_matter(text: str) -> tuple[dict[str, str], list[tuple[int, str]], bool]:
    """分离 YAML front-matter 与正文。

    只有「首行 ``---`` + 找到闭合 ``---`` + 区间内至少一行形如 ``key: value``」三者同时成立才
    认定为 front-matter。加最后一条是因为 Markdown 允许首行用 ``---`` 作主题分隔线，那种情况下
    把后续内容当 front-matter 会整段吞掉正文（既漏检又误报基线缺失）。

    参数:
        text: 文档全文。

    返回:
        (front_matter, 正文行列表, 是否存在 front-matter)。正文行带原始 1-based 行号。
    """

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, list(enumerate(lines, start=1)), False

    end: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None or not any(FRONT_MATTER_KEY_RE.match(line) for line in lines[1:end]):
        return {}, list(enumerate(lines, start=1)), False

    front_matter: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" in line:
            key, value = line.split(":", 1)
            front_matter[key.strip()] = value.strip().strip('"').strip("'")

    body = [(index + 1, lines[index]) for index in range(end + 1, len(lines))]
    return front_matter, body, True


def scan_blocks(
    body: list[tuple[int, str]],
) -> tuple[list[Block], list[tuple[int, str]], int | None]:
    """切分代码围栏与普通正文。

    参数:
        body: 带行号的正文行。

    返回:
        (代码块列表, 普通正文行列表, 未闭合围栏的起始行号或 None)。只有普通正文行参与链接、
        指针与小节检查——围栏内的 mermaid 语法与示例代码不应按正文校验。
    """

    blocks: list[Block] = []
    plain: list[tuple[int, str]] = []
    opened: dict[str, object] | None = None

    for position, (lineno, line) in enumerate(body):
        match = FENCE_RE.match(line)
        if match:
            marker = match.group(1)[0]
            run_length = len(match.group(1))
            info = match.group(2).strip()
            if opened is None:
                opened = {
                    "position": position,
                    "line": lineno,
                    "info": info,
                    "marker": marker,
                    "run": run_length,
                }
                continue
            closes_open_fence = (
                marker == opened["marker"]
                and run_length >= int(opened["run"])  # type: ignore[arg-type]
                and not info
            )
            if closes_open_fence:
                start_position = int(opened["position"])  # type: ignore[arg-type]
                blocks.append(
                    Block(
                        start=int(opened["line"]),  # type: ignore[arg-type]
                        end=lineno,
                        lang=str(opened["info"]).split()[0] if str(opened["info"]) else "",
                        lines=[text for _, text in body[start_position + 1 : position]],
                    )
                )
                opened = None
                continue
            # 围栏行本身不进入正文：它既不是内容，也不该被当作新的开始。
            continue
        if opened is None:
            plain.append((lineno, line))

    unclosed = int(opened["line"]) if opened is not None else None  # type: ignore[arg-type]
    return blocks, plain, unclosed


def is_pointer_token(token: str) -> bool:
    """判断反引号内容是否值得当作文件路径指针去核对。

    刻意写得保守：只认「含 ``/`` 且以已知后缀结尾」或「以 ``/`` 结尾的目录形态」，其余一律跳过。
    ``~``、绝对路径开头、含 ``=``/``$``/``*`` 的写法都排除——它们通常指向仓库外的运行时位置
    或是占位符，核对必然失败。宁可漏检，也不要制造无意义的报错。
    """

    if not token or len(token) > 300:
        return False
    if any(ch.isspace() for ch in token):
        return False
    if any(ch in token for ch in "*?<>{}|=$!~"):
        return False
    if token.startswith(("/", "-")):
        return False
    if token in ROOT_FILE_ALLOWLIST:
        return True

    base = token.split(":", 1)[0]
    if not base:
        return False
    if base.endswith("/"):
        return "/" in base.rstrip("/")
    if "/" not in base:
        return False
    return base.endswith(SOURCE_SUFFIXES)


def normalize_symbol(suffix: str) -> list[str]:
    """把 ``Foo.bar()`` 这类写法归一化成分段符号名列表。

    去掉调用括号、泛型尖括号与下标，忽略空段。不还原作用域，只做「名字是否出现过」的核对。
    """

    cleaned = re.sub(r"\(.*\)$", "", suffix.strip())
    cleaned = re.sub(r"[<>\[\]]", "", cleaned)
    return [part for part in cleaned.split(".") if part]


class WikiChecker:
    """对单个 wiki 目录执行全部机械校验。"""

    def __init__(
        self,
        *,
        root: Path,
        wiki_dir: Path,
        max_code_block_lines: int = DEFAULT_MAX_CODE_BLOCK_LINES,
        skip_git_checks: bool = False,
        require_pages: bool = False,
    ) -> None:
        self.root = root.resolve()
        self.wiki_dir = wiki_dir.resolve()
        self.max_code_block_lines = max_code_block_lines
        self.skip_git_checks = skip_git_checks
        self.require_pages = require_pages
        # 指针允许两种基准：仓库根，以及 apps/backend 简写（仓内文档常用）。
        self.pointer_roots = (self.root, self.root / "apps" / "backend")
        self._symbol_cache: dict[Path, set[str] | None] = {}
        self._line_count_cache: dict[Path, int | None] = {}
        self._git_usable: bool | None = None
        self._git_path: str | None = None
        self._git_path_resolved = False

    # ---------- 对外入口 ----------

    def check(self) -> list[Issue]:
        """校验整个 wiki 目录，返回按严重程度排序的问题列表。"""

        pages = sorted(p for p in self.wiki_dir.rglob("*.md") if p.is_file())
        issues: list[Issue] = []

        if not pages:
            if self.require_pages:
                # 终检要求必须有文档，故用 error 级规则；与 warning 级的 NO_PAGES 区分，
                # 以免「还没开始写」和「终检时一篇都没有」共用同一个退出口径。
                issues.append(
                    Issue(
                        "PAGES_REQUIRED",
                        self._display(self.wiki_dir),
                        None,
                        "未发现任何 Markdown 文档；终检要求至少一篇",
                    )
                )
                return issues
            issues.append(
                Issue(
                    "NO_PAGES",
                    self._display(self.wiki_dir),
                    None,
                    "未发现任何 Markdown 文档（契约冻结完成前属预期）",
                )
            )
            return issues

        if not self.skip_git_checks and not self._git_available():
            issues.append(
                Issue(
                    "GIT_UNAVAILABLE",
                    self._display(self.wiki_dir),
                    None,
                    "未找到可用的 git，已跳过 baseline_commit 存在性校验",
                )
            )

        for page in pages:
            issues.extend(self._check_page(page))

        issues.sort(key=lambda item: (SEVERITY_ORDER[item.severity], item.file, item.line or 0))
        return issues

    # ---------- 单篇校验 ----------

    def _check_page(self, page: Path) -> list[Issue]:
        display = self._display(page)
        try:
            text = page.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return [Issue("CHECK_SKIPPED", display, None, f"无法读取文档: {exc}")]

        front_matter, body, _has_front_matter = split_front_matter(text)
        blocks, plain, unclosed = scan_blocks(body)
        is_exempt = page.name in EXEMPT_FILENAMES

        issues: list[Issue] = []

        if unclosed is not None:
            issues.append(
                Issue("FENCE_UNBALANCED", display, unclosed, "代码围栏未闭合")
            )

        issues.extend(self._check_code_blocks(display, blocks))

        if not is_exempt:
            issues.extend(self._check_sections(display, plain))
            issues.extend(self._check_mermaid(display, page, front_matter, blocks))

        issues.extend(self._check_links(display, page, plain))
        issues.extend(self._check_pointers(display, page, plain))
        issues.extend(self._check_docs_reference(display, plain))
        issues.extend(self._check_baseline(display, front_matter, is_exempt))
        return issues

    def _check_sections(self, display: str, plain: list[tuple[int, str]]) -> list[Issue]:
        headings = []
        for _lineno, line in plain:
            match = HEADING_RE.match(line)
            if match is not None:
                headings.append(NUMBERING_RE.sub("", match.group(2).strip()))

        issues: list[Issue] = []
        for canonical, prefixes in SECTION_PREFIXES.items():
            if not any(heading.startswith(prefixes) for heading in headings):
                issues.append(
                    Issue("SECTION_MISSING", display, None, f"缺少模板小节的标题「{canonical}」")
                )
        return issues

    def _check_mermaid(
        self,
        display: str,
        page: Path,
        front_matter: dict[str, str],
        blocks: list[Block],
    ) -> list[Issue]:
        doc_type = front_matter.get("type", "").split()[0:1]
        is_flow = doc_type == ["flow"] or "链路" in page.name
        if not is_flow:
            return []
        if any(block.lang == "mermaid" for block in blocks):
            return []
        return [
            Issue(
                "MERMAID_MISSING",
                display,
                None,
                "链路类文档建议配一张 mermaid 图（flowchart 或 sequenceDiagram）",
            )
        ]

    def _check_code_blocks(self, display: str, blocks: list[Block]) -> list[Issue]:
        issues: list[Issue] = []
        for block in blocks:
            if block.lang == "mermaid":
                continue
            length = len(block.lines)
            while length > 0 and not block.lines[length - 1].strip():
                length -= 1
            if length > self.max_code_block_lines:
                issues.append(
                    Issue(
                        "CODE_BLOCK_LONG",
                        display,
                        block.start,
                        f"第 {block.start}-{block.end} 行代码块 {length} 行，超过阈值 "
                        f"{self.max_code_block_lines} 行；建议改为路径指针",
                    )
                )
        return issues

    def _check_links(
        self, display: str, page: Path, plain: list[tuple[int, str]]
    ) -> list[Issue]:
        issues: list[Issue] = []
        for lineno, line in plain:
            for _label, target in LINK_RE.findall(line):
                if not target or target.startswith(EXTERNAL_TARGET_PREFIXES):
                    continue
                if target.startswith("#"):
                    continue
                path_part = target.split("#", 1)[0].strip()
                if not path_part or any(ch in path_part for ch in "<>{}"):
                    continue
                if not self._resolve_link(page, path_part):
                    issues.append(
                        Issue("LINK_MISSING", display, lineno, f"相对链接目标不存在: {target}")
                    )
        return issues

    def _check_pointers(
        self, display: str, page: Path, plain: list[tuple[int, str]]
    ) -> list[Issue]:
        issues: list[Issue] = []
        for lineno, line in plain:
            for token in BACKTICK_RE.findall(line):
                if not is_pointer_token(token):
                    continue
                base, separator, suffix = token.partition(":")
                resolved = self._resolve_pointer(page, base)
                if resolved is None:
                    issues.append(
                        Issue(
                            "POINTER_MISSING",
                            display,
                            lineno,
                            f"路径指针指向的文件不存在: {base}",
                        )
                    )
                    continue
                if separator and suffix:
                    issues.extend(
                        self._check_pointer_suffix(display, lineno, resolved, suffix, token)
                    )
        return issues

    def _check_pointer_suffix(
        self, display: str, lineno: int, resolved: Path, suffix: str, token: str
    ) -> list[Issue]:
        line_match = LINE_REF_RE.match(suffix)
        if line_match:
            return self._check_line_ref(display, lineno, resolved, line_match, token)
        if resolved.suffix != ".py":
            return []
        return self._check_symbol_ref(display, lineno, resolved, suffix, token)

    def _check_line_ref(
        self,
        display: str,
        lineno: int,
        resolved: Path,
        match: re.Match[str],
        token: str,
    ) -> list[Issue]:
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        total = self._line_count(resolved)
        if total is None:
            return [
                Issue(
                    "CHECK_SKIPPED",
                    display,
                    lineno,
                    f"{token} 无法统计行数，行号范围未校验",
                )
            ]
        in_range = 1 <= start <= total and start <= end <= total
        if in_range:
            return []
        return [
            Issue(
                "LINE_REF_OUT_OF_RANGE",
                display,
                lineno,
                f"{token} 的行号超出文件范围（该文件共 {total} 行）",
            )
        ]

    def _check_symbol_ref(
        self, display: str, lineno: int, resolved: Path, suffix: str, token: str
    ) -> list[Issue]:
        symbols = self._symbols(resolved)
        if symbols is None:
            return [
                Issue(
                    "CHECK_SKIPPED",
                    display,
                    lineno,
                    f"{token} 的目标文件无法解析为 Python 语法树，符号未校验",
                )
            ]
        missing = [part for part in normalize_symbol(suffix) if part not in symbols]
        if not missing:
            return []
        return [
            Issue(
                "SYMBOL_MISSING",
                display,
                lineno,
                f"{token} 的符号未在源码中出现: {', '.join(missing)}",
            )
        ]

    def _check_docs_reference(
        self, display: str, plain: list[tuple[int, str]]
    ) -> list[Issue]:
        issues: list[Issue] = []
        for lineno, line in plain:
            if DOCS_REFERENCE_RE.search(line):
                issues.append(
                    Issue(
                        "DOCS_REFERENCE",
                        display,
                        lineno,
                        "文中出现 docs/ 字样；该目录是已过时的历史计划，勿作为依据",
                    )
                )
        return issues

    def _check_baseline(
        self,
        display: str,
        front_matter: dict[str, str],
        is_exempt: bool,
    ) -> list[Issue]:
        if is_exempt:
            return []
        baseline = front_matter.get("baseline_commit", "")
        if not baseline:
            return [
                Issue(
                    "BASELINE_MISSING",
                    display,
                    None,
                    "缺少 front-matter baseline_commit，无法判断文档基于哪次代码快照",
                )
            ]
        if not BASELINE_SHA_RE.match(baseline):
            return [
                Issue(
                    "BASELINE_INVALID",
                    display,
                    None,
                    f"baseline_commit 不是合法 commit sha: {baseline}",
                )
            ]
        if self.skip_git_checks or not self._git_available():
            return []
        if not self._commit_exists(baseline):
            return [
                Issue(
                    "BASELINE_UNKNOWN",
                    display,
                    None,
                    f"baseline_commit 不在当前 git 仓库中: {baseline}",
                )
            ]
        return []

    # ---------- 解析与缓存 ----------

    def _resolve_link(self, page: Path, target: str) -> Path | None:
        for candidate in (page.parent / target, self.root / target, self.wiki_dir / target):
            if candidate.exists():
                return candidate
        return None

    def _resolve_pointer(self, page: Path, base: str) -> Path | None:
        for root in self.pointer_roots:
            candidate = root / base
            if candidate.exists():
                return candidate
        candidate = page.parent / base
        if candidate.exists():
            return candidate
        return None

    def _line_count(self, path: Path) -> int | None:
        if path in self._line_count_cache:
            return self._line_count_cache[path]
        try:
            total = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            self._line_count_cache[path] = None
            return None
        self._line_count_cache[path] = total
        return total

    def _symbols(self, path: Path) -> set[str] | None:
        """收集 Python 文件里可供指针引用的名字。

        覆盖：函数/类定义、赋值与注解赋值的名字、**赋值目标的属性名**（``self.x = ...``）、
        以及 ``import`` / ``from ... import`` 的绑定名。刻意收得宽：这是 warning 级启发式检查，
        误报的代价高于漏报。仍然不做作用域还原，因此 ``Foo.bar`` 这类只能验到「名字出现过」。
        """

        if path in self._symbol_cache:
            return self._symbol_cache[path]
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, ValueError):
            self._symbol_cache[path] = None
            return None

        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                names.add(node.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    names.add(alias.asname or alias.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    names.update(_assigned_names(target))
            elif isinstance(node, ast.AnnAssign | ast.AugAssign):
                names.update(_assigned_names(node.target))
            elif isinstance(node, ast.arg):
                names.add(node.arg)

        self._symbol_cache[path] = names
        return names

    def _commit_exists(self, revision: str) -> bool:
        return self._run_git(["cat-file", "-e", f"{revision}^{{commit}}"])

    def _git_available(self) -> bool:
        if self._git_usable is None:
            self._git_usable = (
                self._git_executable() is not None
                and self._run_git(["rev-parse", "--git-dir"])
            )
        return self._git_usable

    def _run_git(self, arguments: list[str]) -> bool:
        """执行只读 git 命令；git 缺失或超时一律降级为 False，不让脚本崩溃。

        用 ``shutil.which`` 解析出的绝对路径启动进程，避免 PATH 被劫持；参数全是固定字面量
        加已通过 ``BASELINE_SHA_RE`` 校验的 revision，不接受外部拼接的任意命令。
        """

        executable = self._git_executable()
        if executable is None:
            return False
        try:
            result = subprocess.run(  # noqa: S603
                [executable, "-C", str(self.root), *arguments],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def _git_executable(self) -> str | None:
        if not self._git_path_resolved:
            self._git_path = shutil.which("git")
            self._git_path_resolved = True
        return self._git_path

    def _display(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()


def _assigned_names(target: ast.expr) -> set[str]:
    """从赋值目标里取出可引用的名字，覆盖 ``a = b = x`` 与 ``self.attr = x``。"""

    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Attribute):
        return {target.attr}
    if isinstance(target, ast.Tuple | ast.List):
        names: set[str] = set()
        for element in target.elts:
            names.update(_assigned_names(element))
        return names
    return set()


# ---------- 输出 ----------


def format_text(issues: list[Issue], *, root: Path, wiki_dir: Path, page_count: int) -> str:
    lines = [
        "wiki 机械校验",
        f"  仓库根  : {root}",
        f"  文档目录: {wiki_dir}",
        f"  文档数  : {page_count}",
        "",
    ]
    if not issues:
        lines.append("未发现问题。")
    current_file = None
    for issue in issues:
        if issue.file != current_file:
            current_file = issue.file
            lines.append(current_file)
        location = f" 第 {issue.line} 行" if issue.line else ""
        lines.append(f"  [{issue.severity}] {issue.code}{location}: {issue.message}")

    errors = sum(1 for item in issues if item.severity == ERROR)
    warnings = sum(1 for item in issues if item.severity == WARNING)
    lines.extend(["", f"汇总: {errors} error, {warnings} warning（warning 不影响退出码）"])
    lines.append("已知盲区: 4 空格缩进代码块、图片链接、类作用域符号不校验。")
    return "\n".join(lines)


def format_rule_table() -> str:
    lines = ["规则表（严重程度的事实源）", ""]
    for code in sorted(RULE_SEVERITY, key=lambda item: (SEVERITY_ORDER[RULE_SEVERITY[item]], item)):
        lines.append(f"  [{RULE_SEVERITY[code]:7s}] {code:24s} {RULE_DOC.get(code, '')}")
    return "\n".join(lines)


def issues_to_json(issues: list[Issue], *, root: Path, wiki_dir: Path, page_count: int) -> str:
    return json.dumps(
        {
            "root": str(root),
            "wiki_dir": str(wiki_dir),
            "page_count": page_count,
            "summary": {
                "errors": sum(1 for item in issues if item.severity == ERROR),
                "warnings": sum(1 for item in issues if item.severity == WARNING),
            },
            "issues": [
                {
                    "code": item.code,
                    "severity": item.severity,
                    "file": item.file,
                    "line": item.line,
                    "message": item.message,
                }
                for item in issues
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


# ---------- 自检 ----------

_BASE_DOC = """---
baseline_commit: {commit}
type: {doc_type}
---

# {title}

## 定位

示例模块的说明，不负责其他内容。

## 关键文件

| 路径 | 职责 |
| --- | --- |
| `apps/backend/app/sample.py` | 示例实现 |

## 上下游契约

入口见 `apps/backend/app/sample.py:Sample`。

## 不变量与坑

{extra}

## 相关文档

- [总览](README.md)
"""

_SAMPLE_PY = '''"""示例模块。"""

import os
from collections import OrderedDict as OD


class Sample:
    """示例类。"""

    def method(self) -> None:
        self._locks = {}
'''


def _probe_doc(
    commit: str, *, extra: str = "无。", doc_type: str = "module", title: str = "示例"
) -> str:
    return _BASE_DOC.format(commit=commit, doc_type=doc_type, title=title, extra=extra)


def _fenced(lines: list[str], lang: str = "") -> str:
    return "\n".join(["```" + lang, *lines, "```"])


def _selftest_cases(commit: str) -> list[tuple[str, str, str, set[str]]]:
    """返回 (用例名, 文件名, 文档内容, 期望命中的问题码集合)。

    断言用**精确集合**而非「包含」：既能验证规则会触发，也能验证合法写法不会产生任何问题——
    后者才是防误报的关键，也是上一版自检最大的漏洞。
    """

    def doc(
        extra: str = "无。",
        doc_type: str = "module",
        title: str = "示例",
        commit_value: str | None = None,
    ) -> str:
        return _probe_doc(
            commit if commit_value is None else commit_value,
            extra=extra,
            doc_type=doc_type,
            title=title,
        )

    return [
        # ---- 负例：每条 error 规则 ----
        (
            "坏链接",
            "02-bad-link.md",
            doc().replace("(README.md)", "(README-gone.md)"),
            {"LINK_MISSING"},
        ),
        (
            "指针不存在",
            "03-bad-pointer.md",
            doc(extra="见 `apps/backend/app/missing.py`。"),
            {"POINTER_MISSING"},
        ),
        # 未闭合围栏放在文末：若插在中间会吞掉其后的小节标题，连带触发 SECTION_MISSING，
        # 掩盖这条规则本身的验证。
        (
            "围栏未闭合",
            "06-unbalanced.md",
            doc() + "\n```python\nprint('x')\nprint('y')\n",
            {"FENCE_UNBALANCED"},
        ),
        ("基线不合法", "10-bad-baseline.md", doc(commit_value="not-a-sha"), {"BASELINE_INVALID"}),
        # ---- 负例：warning 规则 ----
        (
            "符号不存在",
            "04-bad-symbol.md",
            doc(extra="见 `apps/backend/app/sample.py:NoSuchSymbol`。"),
            {"SYMBOL_MISSING"},
        ),
        (
            "行号越界",
            "05-bad-line.md",
            doc(extra="见 `apps/backend/app/sample.py:99999`。"),
            {"LINE_REF_OUT_OF_RANGE"},
        ),
        (
            "代码块过长",
            "07-long-block.md",
            doc(extra=_fenced([f"print({i})" for i in range(20)], "python")),
            {"CODE_BLOCK_LONG"},
        ),
        (
            "缺少小节",
            "08-no-section.md",
            doc().replace("## 不变量与坑\n\n无。\n", ""),
            {"SECTION_MISSING"},
        ),
        (
            "缺少基线",
            "09-no-baseline.md",
            doc().replace(f"baseline_commit: {commit}", "title: 无基线"),
            {"BASELINE_MISSING"},
        ),
        (
            "提及 docs",
            "11-docs-ref.md",
            doc(extra="禁止引用 `docs/`。"),
            {"DOCS_REFERENCE"},
        ),
        ("链路页缺 mermaid", "12-链路-示例.md", doc(), {"MERMAID_MISSING"}),
        # ---- 正例：合法写法必须零问题（防误报） ----
        ("合法内容页", "01-ok.md", doc(), set()),
        (
            "合法变体标题",
            "13-variant-heading.md",
            doc().replace("## 上下游契约", "## 上下游"),
            set(),
        ),
        (
            "合法 info 串围栏",
            "14-info-fence.md",
            doc(extra=_fenced(["print(1)", "print(2)"], "python title=sample.py")),
            set(),
        ),
        (
            "合法调用与导入名",
            "15-call-pointer.md",
            doc(
                extra=(
                    "见 `apps/backend/app/sample.py:Sample.method()`、"
                    "`apps/backend/app/sample.py:os`、`apps/backend/app/sample.py:OD`。"
                )
            ),
            set(),
        ),
        (
            "合法仓库外路径",
            "16-home-path.md",
            doc(extra="配置见 `~/.cosir/agents/wiki-writer.json`。"),
            set(),
        ),
        (
            "合法中等代码块",
            "17-medium-block.md",
            doc(extra=_fenced([f"行{i}" for i in range(8)], "yaml")),
            set(),
        ),
        (
            "合法首行分隔线",
            "18-leading-rule.md",
            "---\n\n# 标题\n\n## 定位\n\n说明。\n\n## 关键文件\n\n"
            "见 `apps/backend/app/sample.py`。\n\n## 上下游契约\n\n无。\n\n"
            "## 不变量与坑\n\n无。\n\n## 相关文档\n\n- [总览](README.md)\n",
            {"BASELINE_MISSING"},
        ),
    ]


def _conventions_page() -> tuple[str, str, set[str]]:
    """规范页：豁免小节与基线要求，但展示模板的长代码块仍会被提示（warning 不阻断）。

    该页刻意既无 ``baseline_commit`` 也不含五个内容小节——若豁免失效，会额外出现
    ``BASELINE_MISSING`` 与 ``SECTION_MISSING``；因此期望集合能同时验证豁免是否生效。
    """

    content = "# 约定\n\n## 模板\n\n" + _fenced([f"模板第{i}行" for i in range(18)]) + "\n"
    return content, {"CODE_BLOCK_LONG"}


def run_selftest() -> int:
    """在临时目录构造正反用例，断言每条规则的**精确**命中集合。"""

    with tempfile.TemporaryDirectory(prefix="wiki-check-") as tmp:
        root = Path(tmp)
        wiki_dir = root / "wiki"
        wiki_dir.mkdir(parents=True)
        sample = root / "apps" / "backend" / "app" / "sample.py"
        sample.parent.mkdir(parents=True)
        sample.write_text(_SAMPLE_PY, encoding="utf-8")
        (wiki_dir / "README.md").write_text("# 索引\n\n- [示例](01-ok.md)\n", encoding="utf-8")

        commit = "0" * 40
        cases = _selftest_cases(commit)
        cases.append(("规范页豁免", "CONVENTIONS.md", *_conventions_page()))

        for _name, filename, content, _expected in cases:
            (wiki_dir / filename).write_text(content, encoding="utf-8")

        checker = WikiChecker(root=root, wiki_dir=wiki_dir, skip_git_checks=True)
        issues = checker.check()

        actual: dict[str, set[str]] = {}
        for issue in issues:
            actual.setdefault(issue.file, set()).add(issue.code)

        failures: list[str] = []
        for name, filename, _content, expected in cases:
            got = actual.get(f"wiki/{filename}", set())
            if got != expected:
                want = sorted(expected) or "无"
                have = sorted(got) or "无"
                failures.append(f"{name}: 期望 {want}，实际 {have}")

        print(f"自检用例: {len(cases)}")
        for name, filename, _content, expected in cases:
            failed = any(item.startswith(f"{name}:") for item in failures)
            state = "FAIL" if failed else "PASS"
            want = sorted(expected) or "无问题"
            print(f"  [{state}] {name} ({filename}) -> {want}")
        if failures:
            print("\n失败明细:")
            for item in failures:
                print(f"  - {item}")
            return 1
        print("\n全部用例通过。")
        return 0


# ---------- 入口 ----------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_wiki.py",
        description="wiki 文档集的机械校验（轻量辅助，仅标准库）。",
    )
    parser.add_argument("--root", default=".", help="仓库根目录，默认当前目录")
    parser.add_argument("--wiki-dir", default=None, help="文档目录，默认 <root>/wiki")
    parser.add_argument(
        "--max-code-block-lines",
        type=int,
        default=DEFAULT_MAX_CODE_BLOCK_LINES,
        help=f"非 mermaid 代码块行数阈值，默认 {DEFAULT_MAX_CODE_BLOCK_LINES}",
    )
    parser.add_argument("--strict", action="store_true", help="warning 也导致非零退出码")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出，便于程序消费")
    parser.add_argument(
        "--require-pages", action="store_true", help="文档数为 0 时报错（终检场景使用）"
    )
    parser.add_argument("--skip-git-checks", action="store_true", help="跳过 git 相关校验")
    parser.add_argument("--list-rules", action="store_true", help="输出规则表与严重程度")
    parser.add_argument("--self-test", action="store_true", help="运行内置自检用例")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_rules:
        print(format_rule_table())
        return 0
    if args.self_test:
        return run_selftest()

    root = Path(args.root).resolve()
    wiki_dir = Path(args.wiki_dir).resolve() if args.wiki_dir else root / "wiki"
    if not wiki_dir.is_dir():
        # 配置错误与「文档有问题」必须可区分，故与 argparse 一致返回 2。
        print(f"文档目录不存在: {wiki_dir}", file=sys.stderr)
        return 2

    checker = WikiChecker(
        root=root,
        wiki_dir=wiki_dir,
        max_code_block_lines=args.max_code_block_lines,
        skip_git_checks=args.skip_git_checks,
        require_pages=args.require_pages,
    )
    issues = checker.check()
    page_count = sum(1 for _ in wiki_dir.rglob("*.md"))

    if args.json:
        print(issues_to_json(issues, root=root, wiki_dir=wiki_dir, page_count=page_count))
    else:
        print(format_text(issues, root=root, wiki_dir=wiki_dir, page_count=page_count))

    errors = sum(1 for item in issues if item.severity == ERROR)
    warnings = sum(1 for item in issues if item.severity == WARNING)
    if errors:
        return 1
    if args.strict and warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
