"""灾难级命令硬拒绝检测（终端执行链专属前置裁决）。

本模块只做命令文本的模式检测并给出裁决，不执行命令、不读写文件、不做路径
解析。检测为纯函数、零 I/O，供 ``execute_terminal`` 与未来 ``execute_code``
复用。deny-list 覆盖不可逆、全局破坏、远程注入执行类灾难级命令；其中删除类
命令（``rm`` / ``del`` / ``erase`` / ``rd`` / ``rmdir`` / ``Remove-Item`` /
``find -delete`` / ``xargs rm`` 等）一律硬拒，强制模型走受控的
``delete`` 工具（workspace 作用域、不审批、含
路径防逃逸与仅空目录/显式递归两道闸），与"终端是原始 shell 通道、命令可逃逸
workspace、静态判定作用域不可靠"的前次决策一致。不可逆 git 操作（``reset --hard`` /
``push --force`` / ``clean -f`` / ``checkout --`` 丢弃未提交 / ``branch -D`` /
``config --global`` / ``commit --amend``）同样纳入 deny-list（见
``_GIT_DESTRUCTIVE_PATTERNS``），与删除类命令一致强制拦截；其余普通危险
（``pip install`` 等）仍放行（免审批模型、本机自担风险）。

抗变形绕过：匹配前先 ``preprocess_command`` 做不可逆形态归一化（NFKC / IFS
展开 / 续行合并 / 注释剥离 / 命令替换标记），降低经典变形绕过成功率。预处理
是尽力而为的纵深防御，不宣称能挡住所有变形（见 §11 风险）。
"""

import re
import unicodedata
from dataclasses import dataclass

# 灾难级 deny-list：每项严格对应一条 (regex, key, description)。
# key 为稳定分类键（英文 snake_case），供日志 event/data 使用；description 拼进
# 返回给模型的 error。正则经 re.IGNORECASE 编译一次缓存（见 _COMPILED）。
# 删除类命令一律硬拒：原始 shell 通道无法静态可靠判定命令作用域（可 cd / 绝对路径
# 逃逸 workspace），故统一拒绝、强制走受控 delete 工具。
_DANGEROUS_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (
        r"\b(?:del|erase)\b",
        "windows_del",
        "del/erase 命令已被禁用，请改用 delete 工具删除文件",
    ),
    (
        r"\b(?:rd|rmdir)\b",
        "windows_rd",
        "rd/rmdir 命令已被禁用，请改用 delete 工具删除目录",
    ),
    (
        r"\b(?:powershell|pwsh)\b[^|]*-Command|Remove-Item|-rm",
        "powershell_remove_item",
        "PowerShell Remove-Item 删除已被禁用，请改用 delete 工具",
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
        "find -exec[dir] rm 删除已被禁用，请改用 delete 工具",
    ),
    (
        r"\bfind\b[^|]*-delete\b",
        "find_delete",
        "find -delete 删除已被禁用，请改用 delete 工具",
    ),
    (
        r"\b(?:xargs|xargs -0)\b[^|]*\brm\b",
        "xargs_rm",
        "xargs rm 删除已被禁用，请改用 delete 工具",
    ),
    (
        r"\brm\b",
        "rm_disabled",
        "rm 命令已被禁用，请改用 delete 工具删除文件",
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

_COMPILED = tuple(
    (re.compile(pattern, re.IGNORECASE), key, description)
    for patterns in (_DANGEROUS_PATTERNS, _GIT_DESTRUCTIVE_PATTERNS)
    for pattern, key, description in patterns
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


def detect_dangerous_command(command: str) -> DangerousCommandVerdict:
    """灾难级命令硬拒绝检测。

    内部先 ``preprocess_command`` 归一化，再匹配约 39 条灾难级模式（含删除类与
    不可逆 git 操作），其余一律放行。

    参数:
        command: 原始命令字符串。

    返回:
        ``DangerousCommandVerdict``：命中即 ``is_dangerous=True`` 且携带稳定
        ``key`` 与 ``description``；未命中 ``is_dangerous=False``、``key=""``、
        ``description=""``。

    异常:
        无。

    副作用:
        无（纯函数、零 I/O）。
    """
    # 全部判定仅基于归一化串（已做 NFKC / IFS 展开 / 续行合并 / 注释剥离）。
    # 不在原始 command 上二次兜底匹配：否则注释内的无害危险字样（如
    # ``git status # rm -rf /``）会被误拒，违背 preprocess_command 的注释剥离语义。
    # 归一化是「增强」匹配（让变形更易被识别），不会丢失真实危险 token；
    # 注释剥离的过度问题由 _strip_comments 对 URL 内 # 的特判兜底（见 _strip_comments）。
    normalized = preprocess_command(command)
    for regex, key, description in _COMPILED:
        if regex.search(normalized):
            return DangerousCommandVerdict(is_dangerous=True, key=key, description=description)
    return DangerousCommandVerdict(is_dangerous=False, key="", description="")


def preprocess_command(command: str) -> str:
    """对命令做不可逆形态归一化，降低经典变形绕过成功率。

    步骤：NFKC 归一 → ${IFS}/$IFS 展开 → 反斜杠续行合并 → 引号感知注释剥离 →
    命令替换标记（保留 ``$(`` / 反引号前缀供替换类规则判定）。

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
