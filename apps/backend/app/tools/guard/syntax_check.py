"""多语言语法检查内核（tree-sitter 驱动，error 驱动前置守卫）。

本模块承载「按扩展名分发到 tree-sitter grammar 做语法检查」的唯一共享实现：
把 ``write_file`` / ``patch_tool`` 中对 ``.py`` 的 ``ast.parse`` 软校验，升级为多语言、
单一来源的语法检查守卫，消除各 handler 的重复实现（Rule of Three）。

职责边界：
- 负责：扩展名 → 语言映射、tree-sitter 解析、遍历 ``ERROR`` / ``MISSING`` 节点收集
  结构化诊断、按诊断生成英文 ``reason`` 自修复引导。
- 不负责：决定是否阻断/是否返回 error（那是调用方按 ``has_error`` 决策）；不构造
  ``ToolObservation`` / ``tool_error``；不 import 任何 ``tool_handler`` / ``tool_execute``
  业务模块（维持 ``guard/`` 横切层零业务依赖）。

设计取舍（已与用户确认）：
- **选型用官方 tree-sitter grammar 包**（``tree-sitter`` 宿主 + ``tree-sitter-python``
  等各语言包），而非 ``tree-sitter-language-pack``：实测后者不支持 ``windows-x86_64``
  平台（仅 linux/macOS 预编译），本桌面端为 Windows；官方 grammar 包提供 Windows
  预编译 wheel，已验证可解析并检测 ``ERROR`` / ``MISSING`` 节点。
- **不依赖本机编译器、不 spawn 子进程**：in-process 解析，任何机器（装没装编译器）
  一致生效。
- **只报语法级 parse error**：不报未定义符号/类型错误（语义诊断属未来 Lint）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Language, Node, Parser

from app.config.logging.logger import log

# 超过该字节数跳过语法检查（避免超大文件无谓解析）。
MAX_CHECK_BYTES = 2 * 1024 * 1024

# 扩展名 → 语言名映射表。新增语言 = 加一行，零改动检查内核。
# 语言名以各官方 grammar 包的 import 名一致为准（tree_sitter_python / tree_sitter_go ...）。
_EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "jsx",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
}


@dataclass(frozen=True)
class SyntaxDiagnostic:
    """单个语法错误诊断（行/列/类型/缺失 token）。"""

    language: str
    # 1-based 行（tree-sitter start_point.row 为 0-based，已 +1 转换）。
    row: int
    # 0-based 列（tree-sitter start_point.column 原样）。
    column: int
    # "unexpected" | "missing"。
    kind: str
    # 人读英文诊断，如 "missing ')' at line 3 col 10"。
    message: str
    # MISSING 节点缺的 token（node.type），驱动 Agent 精准修复。
    expected: str | None = None


@dataclass(frozen=True)
class SyntaxCheckResult:
    """一次语法检查的结果。"""

    # 是否支持该语言（False = 未知扩展名/grammar 加载失败 → 跳过，不视为错误）。
    supported: bool
    has_error: bool = False
    diagnostics: tuple[SyntaxDiagnostic, ...] = ()


# ----------------------------------------------------------------------
# language → tree-sitter Language 构造
# ----------------------------------------------------------------------


def _build_language(name: str) -> Language | None:
    """按语言名构造 tree-sitter ``Language``。

    参数:
        name: 语言名（``python`` / ``javascript`` / ``go`` ...）。

    返回:
        对应的 ``Language``；grammar 包缺失或加载失败时返回 None。

    异常:
        无（所有失败归一化为 None 返回）。

    副作用:
        首次构造时加载对应 grammar 的 native 库。
    """
    try:
        # TypeScript 的 .ts 与 .tsx 复用 typescript 包但取不同 grammar
        # （该包 API 为 language_typescript / language_tsx，无 language()）。
        # 须与其它语言同处 try 内，使 TS grammar 加载失败也走统一降级+日志。
        if name in ("typescript", "tsx"):
            import tree_sitter_typescript as ts

            fn = ts.language_typescript if name == "typescript" else ts.language_tsx
            return Language(fn())
        if name == "python":
            import tree_sitter_python as mod_python

            return Language(mod_python.language())
        if name in ("javascript", "jsx"):
            import tree_sitter_javascript as mod_javascript

            return Language(mod_javascript.language())
        if name == "go":
            import tree_sitter_go as mod_go

            return Language(mod_go.language())
        if name == "rust":
            import tree_sitter_rust as mod_rust

            return Language(mod_rust.language())
        if name == "java":
            import tree_sitter_java as mod_java

            return Language(mod_java.language())
        if name == "html":
            import tree_sitter_html as mod_html

            return Language(mod_html.language())
        if name == "css":
            import tree_sitter_css as mod_css

            return Language(mod_css.language())
        if name in ("c", "cpp"):
            import tree_sitter_c as mod_c

            return Language(mod_c.language())
        if name == "csharp":
            import tree_sitter_c_sharp as mod_csharp

            return Language(mod_csharp.language())
        return None
    except (ImportError, OSError, RuntimeError):
        log.exception(
            "syntax_check_grammar_load_failed",
            extra={
                "msg": "语法检查 grammar 加载失败（非预期路径，需排查）",
                "data": {"language": name},
            },
        )
        return None


# 语言名 → Parser 缓存，避免热路径重复构造 grammar（首次加载 native 库成本较高）。
_PARSER_CACHE: dict[str, Parser] = {}


def _get_parser(name: str) -> Parser | None:
    """取/建语言 parser（带缓存）。

    参数:
        name: 语言名。

    返回:
        可复用的 ``Parser``；grammar 加载失败返回 None。

    异常:
        无。

    副作用:
        首次为某语言构造并缓存 Parser。
    """
    cached = _PARSER_CACHE.get(name)
    if cached is not None:
        return cached
    language = _build_language(name)
    if language is None:
        return None
    parser = Parser(language)
    _PARSER_CACHE[name] = parser
    return parser


def _language_for(path: str) -> str | None:
    """按扩展名查语言名。

    参数:
        path: 文件路径（含扩展名）。

    返回:
        语言名；未知扩展名返回 None。

    异常:
        无。

    副作用:
        无。
    """
    suffix = Path(path).suffix.lower()
    return _EXTENSION_TO_LANGUAGE.get(suffix)


# ----------------------------------------------------------------------
# 诊断收集
# ----------------------------------------------------------------------


def _format_diagnostic(node: Node, language: str) -> SyntaxDiagnostic:
    """把单个 tree-sitter 错误/缺失节点转为结构化诊断。

    参数:
        node: tree-sitter 节点（``ERROR`` 或 ``is_missing``）。
        language: 语言名。

    返回:
        ``SyntaxDiagnostic`` 实例。

    异常:
        无。

    副作用:
        无。
    """
    row = node.start_point.row + 1  # tree-sitter 0-based → 1-based
    column = node.start_point.column
    if node.is_missing:
        token = node.type
        message = f"missing '{token}' at line {row} col {column}"
        return SyntaxDiagnostic(
            language=language,
            row=row,
            column=column,
            kind="missing",
            message=message,
            expected=token,
        )
    message = f"unexpected token at line {row} col {column}"
    return SyntaxDiagnostic(
        language=language,
        row=row,
        column=column,
        kind="unexpected",
        message=message,
    )


def _collect_diagnostics(root: Node, language: str) -> tuple[SyntaxDiagnostic, ...]:
    """遍历语法树收集全部错误/缺失诊断。

    参数:
        root: 语法树根节点。
        language: 语言名。

    返回:
        有序的 ``SyntaxDiagnostic`` 元组。

    异常:
        无。

    副作用:
        无。
    """
    diagnostics: list[SyntaxDiagnostic] = []

    def walk(node: Node) -> None:
        # ERROR 节点与缺失节点同属语法错误；type=="ERROR" 的节点 is_error 恒为 True，
        # 故只需判断 is_error 与 is_missing 两态。
        if node.is_error or node.is_missing:
            diagnostics.append(_format_diagnostic(node, language))
        for child in node.children:
            walk(child)

    walk(root)
    return tuple(diagnostics)


# ----------------------------------------------------------------------
# 对外入口
# ----------------------------------------------------------------------


def check_source_syntax(path: str, content: str | None = None) -> SyntaxCheckResult:
    """检查文件源码语法，返回结构化结果（error 驱动守卫的检查核心）。

    参数:
        path: 文件绝对路径（含扩展名）。
        content: 可选的内存内容；提供时优先解析它，否则读 ``path`` 对应磁盘文件。

    返回:
        ``SyntaxCheckResult``；``supported=False`` 表示「预期跳过」（未知扩展名 / 体积
        超门禁 / 读取不到文件）或「异常降级」（grammar 加载失败 / parse 异常，已写错误
        日志），调用方均不应视为 error；``has_error=True`` 时 ``diagnostics`` 含全部
        错误/缺失诊断（行/列/类型/缺失 token）。

    异常:
        不主动抛出；解析/读取失败归为 ``supported=False`` 或空结果，并写错误日志。

    副作用:
        可能首次加载某语言 grammar 并缓存 parser；可能读取磁盘文件；异常时写日志。
    """
    language = _language_for(path)
    if language is None:
        # 未知扩展名：预期跳过，不记 error 日志（避免噪音）。
        return SyntaxCheckResult(supported=False)

    if content is not None:
        source_bytes = content.encode("utf-8")
    else:
        try:
            source_bytes = Path(path).read_bytes()
        except OSError:
            # 读取失败：非预期路径，写错误日志便于定位。
            log.exception(
                "syntax_check_read_failed",
                extra={"msg": "语法检查读取文件失败", "data": {"path": path}},
            )
            return SyntaxCheckResult(supported=False)
    if len(source_bytes) > MAX_CHECK_BYTES:
        # 体积超门禁：预期跳过，不记 error 日志（避免噪音）。
        return SyntaxCheckResult(supported=False)

    parser = _get_parser(language)
    if parser is None:
        # grammar 加载失败已在 _build_language 写日志；此处直接降级。
        return SyntaxCheckResult(supported=False)

    try:
        tree = parser.parse(source_bytes)
    except Exception:
        # parse 异常：非预期路径，写错误日志便于定位。
        log.exception(
            "syntax_check_parse_failed",
            extra={"msg": "语法检查解析失败", "data": {"path": path, "language": language}},
        )
        return SyntaxCheckResult(supported=False)

    root = tree.root_node
    if not root.has_error:
        return SyntaxCheckResult(supported=True, has_error=False)

    diagnostics = _collect_diagnostics(root, language)
    return SyntaxCheckResult(supported=True, has_error=True, diagnostics=diagnostics)


def format_syntax_reason(result: SyntaxCheckResult) -> str:
    """按诊断生成英文自修复引导（模型可见，驱动 Agent 二次编辑）。

    参数:
        result: 语法检查结果（须 ``has_error=True``）。

    返回:
        面向模型的英文 ``reason``：说明文件已写、给出首个错误的位置与缺失 token、
        引导 Agent 用二次编辑覆盖修复。

    异常:
        无。

    副作用:
        无。
    """
    if not result.has_error or not result.diagnostics:
        return "syntax check reported an error, but no diagnostic detail is available."
    first = result.diagnostics[0]
    detail = first.message
    if first.expected:
        detail = f"missing '{first.expected}' at line {first.row} col {first.column}"
    return (
        f"the written file has a {first.language} syntax error ({detail}); the file has "
        f"been written but is not valid. Fix it with a follow-up edit (write_file or "
        f"patch_tool) that corrects the syntax at that location."
    )


__all__ = [
    "MAX_CHECK_BYTES",
    "SyntaxCheckResult",
    "SyntaxDiagnostic",
    "check_source_syntax",
    "format_syntax_reason",
]
