"""灾难级命令硬拒绝检测（终端执行链专属前置裁决）。

本模块只做命令文本的模式检测并给出裁决，不执行命令、不读写文件、不做路径
解析。检测为纯函数、零 I/O，供 ``execute_terminal`` 与未来 ``execute_code``
复用。deny-list 覆盖不可逆、全局破坏、远程注入执行类灾难级命令；其中删除类
命令（``rm`` / ``del`` / ``erase`` / ``rd`` / ``rmdir`` / ``Remove-Item`` /
``find -delete`` / ``xargs rm`` 等）一律硬拒。workspace 文本文件通过受控的
``delete_file`` 删除；目录删除不受 Agent 文件工具支持。该约束来自"终端是原始 shell
通道、命令可逃逸 workspace、静态判定作用域不可靠"的安全边界。不可逆 git 操作（``reset --hard`` /
``push --force`` / ``clean -f`` / ``checkout --`` 丢弃未提交 / ``branch -D`` /
``config --global`` / ``commit --amend``）同样纳入 deny-list（见
``_GIT_DESTRUCTIVE_PATTERNS``），与删除类命令一致强制拦截；其余普通危险
（``pip install`` 等）仍放行（免审批模型、本机自担风险）。

抗变形绕过：匹配前先 ``preprocess_command`` 做不可逆形态归一化（NFKC / IFS
展开 / 续行合并 / 注释剥离 / 成对引号剥离 / cmd ``^`` 转义剥离），降低经典变形
绕过成功率；解释器 ``-c``/``-e``/``-r`` 代码串（``python -c "..."`` /
``node -e "..."`` / ``php -r "..."`` 等）会被提取后递归做同样的 deny 模式检测，
拦截 ``shutil.rmtree`` / ``fs.rmSync`` / ``os.unlink`` 等代码内删除调用。

**能力边界（重要）**：deny-list 是终端通道的纵深防御层而非完整防线。它只做
静态文本匹配，挡不住所有间接路径（如「下载 + 执行」拆两条命令、编码执行等），
也无法可靠判定命令作用域。文件删除的安全保证依赖更外层：workspace 路径边界 +
受控 ``delete_file`` 文件操作；目录删除不受 Agent 文件工具支持。本模块**不承诺**能拦截全部危险
命令，只负责提高攻击成本；被放行的命令仍受审批与隔离约束。
"""
import re
import unicodedata
from dataclasses import dataclass

from app.config.constant import Constant

# 灾难级 deny-list：每项严格对应一条 (regex, key, description)。
# key 为稳定分类键（英文 snake_case），供日志 event/data 使用；description 拼进
# 返回给模型的 error。正则经 re.IGNORECASE 编译一次缓存（见 _COMPILED）。
# 删除类命令一律硬拒：原始 shell 通道无法静态可靠判定命令作用域（可 cd / 绝对路径
# 逃逸 workspace），故统一拒绝；workspace 文本文件应走 delete_file，目录删除不支持。
_DANGEROUS_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (
        r"\b(?:del|erase)\b",
        "windows_del",
        "del/erase 命令已被禁用，请用 delete_file 删除 workspace 文本文件",
    ),
    (
        r"\b(?:rd|rmdir)\b",
        "windows_rd",
        "rd/rmdir 命令已被禁用，Agent 文件工具不支持删除目录",
    ),
    (
        r"\b(?:powershell|pwsh)\b[^|]*-Command|Remove-Item|-rm",
        "powershell_remove_item",
        "PowerShell Remove-Item 删除已被禁用，请用 delete_file 删除 workspace 文本文件；目录删除不支持",
    ),
    (
        r"\b(?:powershell|pwsh)\b[^|]*-enc(?:odedcommand)?\b",
        "powershell_encoded",
        "PowerShell -EncodedCommand 隐藏载荷",
    ),
    (
        r"\bmkfs(?:\.[a-z0-9]+)?\b",
        "mkfs",
        "格式化文件系统",
    ),
    (
        r"\bformat\s+[a-z]:",
        "format_disk",
        "Windows 格式化盘符",
    ),
    (
        r"\bdd\b[^|]*of=/dev/(?:sd|hd|nvme|vd|disk|loop)[a-z0-9]*\b",
        "dd_block_device",
        "向块设备写（磁盘擦除）",
    ),
    (
        r">\s*/dev/(?:sd|hd|nvme|vd|disk|loop)[a-z0-9]*\b",
        "redirect_block_device",
        "重定向写块设备",
    ),
    (
        r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;",
        "fork_bomb",
        "fork bomb",
    ),
    (
        r"\b(?:curl|wget)\b[^|]*\|\s*(?:ba|z|da)?sh\b",
        "curl_pipe_sh",
        "远程内容管道进 shell",
    ),
    (
        r"\b(?:base64|openssl)\b[^|]*-[dD]\b[^|]*\|\s*(?:ba)?sh\b",
        "decode_pipe_sh",
        "解码后执行",
    ),
    (
        r"\b(?:base32|xxd)\b[^|]*\|\s*(?:ba)?sh\b",
        "other_decode_pipe_sh",
        "base32/xxd 解码执行",
    ),
    (
        r"\becho\b[^|]*\|\s*tr\b[^|]*\|\s*(?:ba)?sh\b",
        "tr_pipe_sh",
        "echo|tr|sh 字符变换执行",
    ),
    (
        r"\$\(\s*(?:curl|wget|base64|base32|xxd)\b",
        "cmd_subst_exec",
        "$(...) 命令替换执行远程/解码内容",
    ),
    (
        r"`\s*(?:curl|wget)\b",
        "backtick_exec",
        "反引号命令替换执行远程内容",
    ),
    (
        r"\beval\b[^|]*\$\(",
        "eval_exec",
        "eval $(...) / eval `...` 执行",
    ),
    (
        r"\bchmod\s+(?:-[^\s]*\s+)*(?:777|666)\b",
        "chmod_world_writable",
        "全局可写权限",
    ),
    (
        r"\bchown\b[^|]*-R[^|]*\broot\b",
        "chown_root",
        "递归提权到 root",
    ),
    (
        r"\bkill\s+-9\s+-1\b",
        "kill_all",
        "杀所有进程",
    ),
    (
        r"\bpkill\b[^|]*-(?:9|f)\b",
        "pkill",
        "pkill -9/-f 广杀",
    ),
    (
        r"\bkillall\b[^|]*-(?:9|r)\b",
        "killall",
        "killall -9/-r 广杀",
    ),
    (
        r"\bsystemctl\b[^|]*(?:stop|restart|disable|mask)\b",
        "systemctl_lifecycle",
        "关停/禁用系统服务",
    ),
    (
        r"\bDROP\s+(?:TABLE|DATABASE)\b",
        "sql_drop",
        "SQL DROP",
    ),
    (
        r"\bDELETE\s+FROM\b(?!.*\bWHERE\b)",
        "sql_delete_no_where",
        "DELETE FROM 无 WHERE（删全表）",
    ),
    (
        r"\bTRUNCATE\b(?:\s+TABLE)?\b",
        "sql_truncate",
        "TRUNCATE TABLE",
    ),
    (
        r"\btee\b[^|]*(/etc/passwd|/etc/shadow|\.env|~/.bashrc|/etc/hosts)",
        "tee_sensitive",
        "覆盖敏感文件",
    ),
    (
        r">\s*(/etc/passwd|/etc/shadow|\.env|~/.bashrc)",
        "redirect_sensitive",
        "重定向覆盖敏感文件",
    ),
    (
        r"\bfind\b[^|]*-exec(?:dir)?\b[^|]*rm\b",
        "find_exec_rm",
        "find -exec[dir] rm 删除已被禁用，请用 delete_file 删除 workspace 文本文件",
    ),
    (
        r"\bfind\b[^|]*-delete\b",
        "find_delete",
        "find -delete 删除已被禁用，请用 delete_file 删除 workspace 文本文件",
    ),
    (
        r"\b(?:xargs|xargs -0)\b[^|]*\brm\b",
        "xargs_rm",
        "xargs rm 删除已被禁用，请用 delete_file 删除 workspace 文本文件",
    ),
    (
        r"\brm\b",
        "rm_disabled",
        "rm 命令已被禁用，请用 delete_file 删除 workspace 文本文件",
    ),
    (
        r"\bunlink\b",
        "unlink_disabled",
        "unlink 命令已被禁用，请用 delete_file 删除 workspace 文本文件",
    ),
)

# 不可逆 / 越权 git 操作 deny-list：与 ``_DANGEROUS_PATTERNS`` 同构 (regex, key, description)。
# 这些操作改写或丢弃 git 历史 / 工作区、改全局配置，误触成本高于普通删除，与"delete 一律
# 走受控工具"的哲学一致——即便 execute_terminal 是高权限需审批通道也不静默放行。
# 纯增量：不与 _DANGEROUS_PATTERNS 的任何条目冲突或覆盖。
# 放行（走终端审批即可，非阻断）：普通 commit(非 amend) / push(非 force) / add / status /
# diff / log / fetch / pull / merge / checkout <分支>(切换) / rebase(含 -i，reflog 可恢复)。
_GIT_DESTRUCTIVE_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (
        r"\bgit\b\s+reset\s+--hard\b",
        "git_reset_hard",
        "git reset --hard discards the working tree and history; use a safe reset or stash instead",
    ),
    (
        r"\bgit\s+push\b[^|]*--force(?:-with-lease)?\b",
        "git_push_force",
        "git push --force overwrites remote history; prefer a normal push or force-with-lease",
    ),
    (
        r"\bgit\s+clean\b[^|]*\s-[fx](?:[dx])?\b",
        "git_clean_force",
        "git clean -f removes untracked files irreversibly; use file tools or git stash instead",
    ),
    (
        r"\bgit\s+checkout\b[^|]*(?:--\s|\s\.\b)",
        "git_checkout_discard",
        "git checkout -- <path> discards uncommitted changes; commit or stash them instead",
    ),
    (
        r"\bgit\s+branch\b[^|]*(?:-D\b|-d\b[^|]*-f\b)",
        "git_branch_delete_force",
        "git branch -D deletes a branch and its history; use -d for merged branches",
    ),
    (
        r"\bgit\s+config\b[^|]*--(?:global|system)\b",
        "git_config_global",
        "git config --global/--system changes out-of-scope global/system config; use --local",
    ),
    (
        r"\bgit\s+commit\b[^|]*--amend\b",
        "git_commit_amend",
        "git commit --amend rewrites committed history; make a new commit instead",
    ),
)

# 解释器代码串（-c/-e/-r/-Command）内的危险调用 deny-list：与命令级模式同构
# (regex, key, description)。命令级 ``\brm\b`` 等词边界不命中 ``rmtree`` /
# ``rmSync`` / ``unlinkSync`` 等代码标识符，故对提取出的代码串单独再跑这一组
# 模式。只作用于代码串内部，不放入命令级 _DANGEROUS_PATTERNS——避免误伤普通
# 命令文本中的同形单词（如 ``git rm`` 已由命令级 \brm\b 覆盖，无需在此重复）。
# 代码串 deny 同样是纵深防御：宁可对删除类调用收紧，也不放行递归删除。
_CODE_DANGEROUS_CALLS: tuple[tuple[str, str, str], ...] = (
    (
        r"\brmtree\b",
        "code_rmtree",
        "代码内 rmtree 递归删除已被禁用，Agent 文件工具不支持删除目录",
    ),
    (
        r"\brmSync\b",
        "code_rm_sync",
        "代码内 fs.rmSync 递归删除已被禁用，Agent 文件工具不支持删除目录",
    ),
    (
        r"\brmdirSync\b",
        "code_rmdir_sync",
        "代码内 fs.rmdirSync 删除已被禁用，Agent 文件工具不支持删除目录",
    ),
    (
        r"\bunlinkSync\b",
        "code_unlink_sync",
        "代码内 fs.unlinkSync 删除已被禁用，请用 delete_file 删除 workspace 文本文件",
    ),
    (
        r"\bunlink\b",
        "code_unlink",
        "代码内 unlink 删除已被禁用，请用 delete_file 删除 workspace 文本文件",
    ),
    (
        r"\bremove\b",
        "code_remove",
        "代码内 remove 删除已被禁用，请用 delete_file 删除 workspace 文本文件",
    ),
    (
        r"\bdelete\b",
        "code_delete",
        "代码内 delete 删除已被禁用，请用 delete_file 删除 workspace 文本文件",
    ),
    (
        r"\bRemove-Item\b",
        "code_remove_item",
        "代码内 Remove-Item 删除已被禁用，请用 delete_file 删除 workspace 文本文件",
    ),
)

# 解释器 -c/-e/-r/-Command 代码串提取正则。匹配 ``python -c "code"`` /
# ``node -e 'code'`` / ``php -r "code"`` / ``powershell -Command "code"`` 等。
# 引号内代码串经 ``(.*?)`` 非贪婪捕获，``(?<!\\)\1`` 拒绝转义引号提前闭合；
# ``re.DOTALL`` 使 ``.`` 可跨换行（多行代码串）。

# 代码串递归检测的最大嵌套深度：防御 ``python -c "python -c ..."`` 无限递归。
_MAX_CODE_RECURSION_DEPTH = 3

_COMPILED = tuple(
    (re.compile(pattern, re.IGNORECASE), key, description)
    for patterns in (_DANGEROUS_PATTERNS, _GIT_DESTRUCTIVE_PATTERNS)
    for pattern, key, description in patterns
)

_CODE_DANGEROUS_CALLS_COMPILED = tuple(
    (re.compile(pattern, re.IGNORECASE), key, description)
    for pattern, key, description in _CODE_DANGEROUS_CALLS
)


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


def detect_dangerous_command(command: str, _depth: int = 0) -> DangerousCommandVerdict:
    """灾难级命令硬拒绝检测。

    内部先 ``preprocess_command`` 归一化，再匹配灾难级模式（含删除类与不可逆
    git 操作）；未命中时若命令含解释器 ``-c/-e/-r/-Command`` 代码串（如
    ``python -c "shutil.rmtree('x')"`` / ``node -e "fs.rmSync('x')"``），提取
    代码串递归执行同样的 deny 检测（``_detect_code_string``），拦截代码内删除
    调用（``rmtree`` / ``rmSync`` / ``unlink`` / ``remove`` / ``delete`` 等）。

    参数:
        command: 原始命令字符串。
        _depth: 代码串递归深度（内部使用，公共调用无需传；防止
            ``python -c "python -c ..."`` 无限递归）。

    返回:
        ``DangerousCommandVerdict``：命中即 ``is_dangerous=True`` 且携带稳定
        ``key`` 与 ``description``；未命中 ``is_dangerous=False``、``key=""``、
        ``description=""``。

    异常:
        无。

    副作用:
        无（纯函数、零 I/O）。
    """
    # 全部判定仅基于归一化串（已做 NFKC / IFS 展开 / 续行合并 / 注释剥离 /
    # 引号剥离 / ^ 剥离）。不在原始 command 上二次兜底匹配：否则注释内的无害
    # 危险字样（如 ``git status # rm -rf /``）会被误拒，违背 preprocess_command
    # 的注释剥离语义。归一化是「增强」匹配（让变形更易被识别），不会丢失真实
    # 危险 token；注释剥离的过度问题由 _strip_comments 对 URL 内 # 的特判兜底
    # （见 _strip_comments）。
    normalized = preprocess_command(command)
    for regex, key, description in _COMPILED:
        if regex.search(normalized):
            return DangerousCommandVerdict(is_dangerous=True, key=key, description=description)
    # 代码串递归：命令级未命中时，提取解释器代码串（基于原始 command，保留引号
    # 边界以便提取）再做代码内危险调用检测。
    if _depth >= _MAX_CODE_RECURSION_DEPTH:
        return DangerousCommandVerdict(is_dangerous=False, key="", description="")
    for code in _extract_interpreter_code(command):
        inner = _detect_code_string(code, _depth + 1)
        if inner.is_dangerous:
            return inner
    return DangerousCommandVerdict(is_dangerous=False, key="", description="")


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


def _extract_interpreter_code(command: str) -> list[str]:
    """提取命令中解释器 ``-c/-e/-r/-Command`` 参数后的引号代码串。

    基于原始 command（保留引号边界）提取，供 ``detect_dangerous_command`` 在
    命令级模式未命中时对代码串做递归 deny 检测。

    参数:
        command: 原始命令字符串。

    返回:
        提取到的代码串列表；无匹配时为空列表。

    异常:
        无。

    副作用:
        无（纯函数）。
    """
    return [match.group(2) for match in Constant.Tools.INTERPRETER_CODE_RE.finditer(command)]


def _detect_code_string(code: str, depth: int) -> DangerousCommandVerdict:
    """对解释器代码串执行同样的 deny 模式检测。

    先归一化代码串，再依次匹配代码串专用危险调用模式（``rmtree`` / ``rmSync`` /
    ``unlink`` / ``remove`` / ``delete`` 等）与命令级模式（代码串内直接出现的
    ``rm -rf`` / ``git reset --hard`` 等文本）；若代码串内部还嵌套解释器调用
    （如 ``os.system("python -c ...")``），递归提取并检测。命中即拒绝，返回
    对应的 ``DangerousCommandVerdict``。

    参数:
        code: 提取出的解释器代码串（含原始引号边界）。
        depth: 当前递归深度，超过 ``_MAX_CODE_RECURSION_DEPTH`` 时停止下钻。

    返回:
        ``DangerousCommandVerdict``：命中即 ``is_dangerous=True``；未命中
        ``is_dangerous=False``、``key=""``、``description=""``。

    异常:
        无。

    副作用:
        无（纯函数）。
    """
    normalized = preprocess_command(code)
    for regex, key, description in _CODE_DANGEROUS_CALLS_COMPILED:
        if regex.search(normalized):
            return DangerousCommandVerdict(is_dangerous=True, key=key, description=description)
    for regex, key, description in _COMPILED:
        if regex.search(normalized):
            return DangerousCommandVerdict(is_dangerous=True, key=key, description=description)
    if depth >= _MAX_CODE_RECURSION_DEPTH:
        return DangerousCommandVerdict(is_dangerous=False, key="", description="")
    for nested in _extract_interpreter_code(code):
        inner = _detect_code_string(nested, depth + 1)
        if inner.is_dangerous:
            return inner
    return DangerousCommandVerdict(is_dangerous=False, key="", description="")
