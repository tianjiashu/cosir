"""灾难级命令硬拒绝检测（终端执行链专属前置裁决）。

本模块只做命令文本的模式检测并给出裁决，不执行命令、不读写文件、不做路径
解析。默认 pattern 仅用于首次生成系统配置；``execute_terminal`` 每次调用都读取当前文件，
并以同一组正则检查归一化后的完整命令文本；用户可在系统配置中增删规则。
默认 deny-list 覆盖不可逆、全局破坏、远程注入执行类命令，其中删除类命令（``rm`` / ``del`` /
``erase`` / ``rd`` / ``rmdir`` / ``Remove-Item`` / ``find -delete`` / ``xargs rm`` 等）
均有拒绝规则。
workspace 文本文件通过受控的
``delete_file`` 删除；目录删除不受 Agent 文件工具支持。该约束来自"终端是原始 shell
通道、命令可逃逸 workspace、静态判定作用域不可靠"的安全边界。不可逆 git 操作（``reset --hard`` /
``push --force`` / ``clean -f`` / ``checkout --`` 丢弃未提交 / ``branch -D`` /
``config --global`` / ``commit --amend``）同样纳入 deny-list（见
``_GIT_DESTRUCTIVE_PATTERNS``），与删除类命令一致强制拦截；其它命令由当前配置中的正则决定。

抗变形绕过：匹配前先 ``preprocess_command`` 做不可逆形态归一化（NFKC / IFS
展开 / 续行合并 / 注释剥离 / 成对引号剥离 / cmd ``^`` 转义剥离），降低经典变形
绕过成功率；解释器 ``-c``/``-e``/``-r`` 代码串仍包含在命令文本内，默认规则会匹配
``shutil.rmtree`` / ``fs.rmSync`` / ``os.unlink`` 等删除调用。

**能力边界（重要）**：deny-list 是终端通道的纵深防御层而非完整防线。它只做
静态文本匹配，挡不住所有间接路径（如「下载 + 执行」拆两条命令、编码执行等），
也无法可靠判定命令作用域。文件删除的安全保证依赖更外层：workspace 路径边界 +
受控 ``delete_file`` 文件操作；目录删除不受 Agent 文件工具支持。本模块**不承诺**能拦截全部危险
命令，只负责提高攻击成本。
"""

import re
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

import regex

# 默认 deny-list 只保存正则字符串；运行时实际规则以系统配置文件为准。
# 默认拒绝删除类命令：原始 shell 通道无法静态可靠判定命令作用域（可 cd / 绝对路径
# 逃逸 workspace），故统一拒绝；workspace 文本文件应走 delete_file，目录删除不支持。
_DANGEROUS_PATTERNS: tuple[str, ...] = (
    "\\b(?:del|erase)\\b",
    "\\b(?:rd|rmdir)\\b",
    "\\b(?:powershell|pwsh)\\b[^|]*-(?:Command|c)\\b|(?:^|[;&|]\\s*)(?:Remove-Item|-rm)\\b",
    "\\b(?:powershell|pwsh)\\b[^|]*-(?:enc(?:odedcommand)?|ec|e)\\b",
    "\\bmkfs(?:\\.[a-z0-9]+)?\\b",
    "\\bformat\\s+[a-z]:",
    "\\bdd\\b[^|]*of=/dev/(?:sd|hd|nvme|vd|disk|loop)[a-z0-9]*\\b",
    ">\\s*/dev/(?:sd|hd|nvme|vd|disk|loop)[a-z0-9]*\\b",
    ":\\(\\)\\s*\\{\\s*:\\s*\\|\\s*:\\s*&\\s*\\}\\s*;",
    "\\b(?:curl|wget)\\b[^|]*\\|\\s*(?:ba|z|da)?sh\\b",
    "\\b(?:base64|openssl)\\b[^|]*-[dD]\\b[^|]*\\|\\s*(?:ba)?sh\\b",
    "\\b(?:base32|xxd)\\b[^|]*\\|\\s*(?:ba)?sh\\b",
    "\\becho\\b[^|]*\\|\\s*tr\\b[^|]*\\|\\s*(?:ba)?sh\\b",
    "\\$\\(\\s*(?:curl|wget|base64|base32|xxd)\\b",
    "`\\s*(?:curl|wget)\\b",
    "\\beval\\b[^|]*\\$\\(",
    "\\bchmod\\s+(?:-[^\\s]*\\s+)*(?:777|666)\\b",
    "\\bchown\\b[^|]*-R[^|]*\\broot\\b",
    "\\bkill\\s+-9\\s+-1\\b",
    "\\bpkill\\b[^|]*-(?:9|f)\\b",
    "\\bkillall\\b[^|]*-(?:9|r)\\b",
    "\\bsystemctl\\b[^|]*(?:stop|restart|disable|mask)\\b",
    "\\bDROP\\s+(?:TABLE|DATABASE)\\b",
    "\\bDELETE\\s+FROM\\b(?!.*\\bWHERE\\b)",
    "\\bTRUNCATE\\b(?:\\s+TABLE)?\\b",
    "\\btee\\b[^|]*(/etc/passwd|/etc/shadow|\\.env|~/.bashrc|/etc/hosts)",
    ">\\s*(/etc/passwd|/etc/shadow|\\.env|~/.bashrc)",
    "\\bfind\\b[^|]*-exec(?:dir)?\\b[^|]*rm\\b",
    "\\bfind\\b[^|]*-delete\\b",
    "\\b(?:xargs|xargs -0)\\b[^|]*\\brm\\b",
    "\\brm\\b",
    "\\bunlink\\b",
)
# 不可逆或作用域超出 workspace 的 Git 操作默认拦截。
_GIT_DESTRUCTIVE_PATTERNS: tuple[str, ...] = (
    "\\bgit\\b\\s+reset\\s+--hard\\b",
    "\\bgit\\s+push\\b[^|]*--force(?:-with-lease)?\\b",
    "\\bgit\\s+clean\\b[^|]*\\s-[fx](?:[dx])?\\b",
    "\\bgit\\s+checkout\\b[^|]*(?:--\\s|\\s\\.\\b)",
    "\\bgit\\s+branch\\b[^|]*(?:-D\\b|-d\\b[^|]*-f\\b)",
    "\\bgit\\s+config\\b[^|]*--(?:global|system)\\b",
    "\\bgit\\s+commit\\b[^|]*--amend\\b",
)
# 解释器代码中的删除 API 规则包含解释器前缀，避免把普通 shell 文本当成代码调用。
_INTERPRETER_CODE_DEFAULT_PATTERNS: tuple[str, ...] = (
    "(?s)\\b(?:python(?:[0-9.]*)?|node(?:js)?|ruby|perl|php|bash|sh|zsh|fish)"
    "\\s+-(?:[cer]\\b).*?\\b(?:rmtree|rmSync|rmdirSync|unlinkSync|unlink|remove)\\s*\\(",
    "(?s)\\b(?:python(?:[0-9.]*)?|node(?:js)?|ruby|perl|php|bash|sh|zsh|fish)"
    "\\s+-(?:[cer]\\b).*?\\bdelete\\s+[\\w$]+\\s*(?:\\.|\\[)",
)
DEFAULT_DENY_PATTERNS: tuple[str, ...] = tuple(
    pattern
    for pattern_group in (
        _DANGEROUS_PATTERNS,
        _GIT_DESTRUCTIVE_PATTERNS,
        _INTERPRETER_CODE_DEFAULT_PATTERNS,
    )
    for pattern in pattern_group
)

_PATTERN_MATCH_TIMEOUT_SECONDS = 0.025
_PATTERN_MATCH_TOTAL_TIMEOUT_SECONDS = 0.5
_MAX_COMMAND_LENGTH = 1_000_000


class DenyPatternError(ValueError):
    """deny-list pattern 无法编译。"""


@dataclass(frozen=True)
class DangerousCommandVerdict:
    """灾难级命令检测结果。

    参数:
        is_dangerous: 是否命中灾难级 deny-list。
        key: 命中分类键（英文 snake_case）；未命中为空串。
        description: 人读说明；未命中为空串。

    返回:
        无。

    异常:
        无。

    副作用:
        无（frozen dataclass，不可变）。
    """

    is_dangerous: bool
    key: str
    description: str


def compile_deny_patterns(patterns: Sequence[str]) -> tuple[regex.Pattern[str], ...]:
    """以忽略大小写语义编译一组 deny-list pattern。"""

    try:
        return tuple(regex.compile(pattern, regex.IGNORECASE) for pattern in patterns)
    except regex.error as exc:
        raise DenyPatternError(str(exc)) from exc


def detect_dangerous_command(
    command: str,
    patterns: Sequence[regex.Pattern[str]],
) -> DangerousCommandVerdict:
    """灾难级命令硬拒绝检测。

    调用方必须传入本次从配置文件读取并编译的 pattern，避免运行期缓存导致配置延迟生效。
    先 ``preprocess_command`` 归一化整条命令，再逐条匹配同一组 pattern。
    解释器 ``-c/-e/-r/-Command`` 的代码仍位于命令文本中，因此规则可以直接命中
    ``python -c "shutil.rmtree('x')"`` / ``node -e "fs.rmSync('x')"`` 等删除调用。

    参数:
        command: 原始命令字符串。
        patterns: 本次执行读取并编译的 pattern 集合。
    返回:
        ``DangerousCommandVerdict``：命中时携带通用拒绝分类；未命中时允许执行。

    异常:
        无。

    副作用:
        无（命令文本匹配本身不读写文件；匹配超时会抛出 ``TimeoutError``，调用方必须拒绝执行）。
    """
    # 全部判定仅基于归一化串（已做 NFKC / IFS 展开 / 续行合并 / 注释剥离 /
    # 引号剥离 / ^ 剥离）。不在原始 command 上二次兜底匹配：否则注释内的无害
    # 危险字样（如 ``git status # rm -rf /``）会被误拒，违背 preprocess_command
    # 的注释剥离语义。归一化是「增强」匹配（让变形更易被识别），不会丢失真实
    # 危险 token；注释剥离的过度问题由 _strip_comments 对 URL 内 # 的特判兜底
    # （见 _strip_comments）。
    if len(command) > _MAX_COMMAND_LENGTH:
        raise TimeoutError("terminal deny-list command exceeds its evaluation size limit")
    if not patterns:
        return DangerousCommandVerdict(is_dangerous=False, key="", description="")
    deadline = time.monotonic() + _PATTERN_MATCH_TOTAL_TIMEOUT_SECONDS
    normalized = preprocess_command(command)
    if _matches_any_pattern(normalized, patterns, deadline):
        return DangerousCommandVerdict(
            is_dangerous=True,
            key="deny_list_pattern",
            description="命令命中终端 deny-list pattern",
        )
    return DangerousCommandVerdict(is_dangerous=False, key="", description="")


def _matches_any_pattern(
    text: str,
    patterns: Sequence[regex.Pattern[str]],
    deadline: float,
) -> bool:
    """在整组共享时限内按顺序搜索 pattern；超时由调用方按拒绝执行处理。"""

    for pattern in patterns:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("terminal deny-list matching exceeded its time budget")
        if pattern.search(text, timeout=min(_PATTERN_MATCH_TIMEOUT_SECONDS, remaining)):
            return True
    return False


def preprocess_command(command: str) -> str:
    """对命令做不可逆形态归一化，降低经典变形绕过成功率。

    步骤：NFKC 归一 → ${IFS}/$IFS 展开 → 反斜杠续行合并 → 引号感知注释剥离 →
    成对引号剥离（``d"e"l`` → ``del``）→ cmd ``^`` 转义剥离（``r^m`` → ``rm``）。

    参数:
        command: 原始命令字符串。

    返回:
        归一化后的命令字符串。

    异常:
        无。

    副作用:
        无（纯函数）。
    """
    text = unicodedata.normalize("NFKC", command)
    # ${IFS} / $IFS / ${IFS,2} 等展开为空格
    text = re.sub(r"\$\{IFS(?:,[^}]*)?\}", " ", text)
    text = re.sub(r"\$IFS\b", " ", text)
    # 行尾反斜杠 + 换行（续行）合并为空格
    text = re.sub(r"\\\r?\n", " ", text)
    # 引号感知注释剥离（http(s):// 中的 # 不视为注释）
    text = _strip_comments(text)
    # 成对引号剥离 + cmd ^ 转义剥离（放在注释剥离之后，避免破坏 # 的引号感知）
    text = _strip_balanced_quotes(text)
    text = text.replace("^", "")
    return text


def _strip_balanced_quotes(text: str) -> str:
    """剥离成对出现的引号字符（``"`` / ``'`` 各出现偶数次视为成对）。

    cmd 允许 ``d"e"l`` 这类把命令名拆开嵌入引号的变形（cmd 解析时引号被吞掉、
    实际执行 ``del``）；同样 POSIX 的 ``r'm'`` 等价 ``rm``。归一化时把同种引号
    出现偶数次（即成对）的引号全部移除，使 ``\bdel\b`` / ``\brm\b`` 等词边界
    模式能够命中。不成对的引号（奇数个，如 ``don't`` 中的 ``'``）保留原样，
    避免破坏正常文本。

    参数:
        text: 待处理的命令字符串。

    返回:
        移除了成对引号后的字符串。

    异常:
        无。

    副作用:
        无（纯函数）。
    """
    # 按种类统计：偶数次（成对）整类移除，奇数次整类保留。该策略对
    # 交错引号（``a"b'c"d'``）也能还原为 ``abcd``，且不破坏 ``don't``。
    for quote in ('"', "'"):
        if text.count(quote) % 2 == 0:
            text = text.replace(quote, "")
    return text


def _strip_comments(text: str) -> str:
    """引号感知的简化注释剥离：从非引号内的 ``#`` 剥到行尾。

    参数:
        text: 待处理的命令字符串。

    返回:
        已剥离行内注释的字符串（保留换行符）。

    异常:
        无。

    副作用:
        无。
    """
    result: list[str] = []
    in_single = False
    in_double = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            result.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            result.append(ch)
        elif ch == "#" and not in_single and not in_double:
            # 仅当 # 位于当前行 URL（含 ://）内时才不当作注释起始，否则从 # 剥到行尾。
            # 用当前行内查找 ://（而非仅 # 前 3 字符）避免 http://x#frag 中的 # 被误剥，
            # 否则会连带丢失其后的危险管道（如 ``| bash``）造成漏检。
            line_start = text.rfind("\n", 0, i) + 1
            if text.rfind("://", line_start, i) != -1:
                result.append(ch)
            else:
                newline = text.find("\n", i)
                if newline == -1:
                    break
                result.append("\n")
                i = newline
        else:
            result.append(ch)
        i += 1
    return "".join(result)
