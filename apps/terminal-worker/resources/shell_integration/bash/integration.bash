# 集成只观察根 shell；输入仍由 readline 与前台程序处理。事件与 zsh 集成一致。
# 本文件只定义钩子；DEBUG trap 与 PROMPT_COMMAND 的安装在 bashrc 顶层完成（原因见 bashrc）。
# 必须同时兼容 macOS 自带 bash 3.2 与 bash 5.x：不用 printf -v 以外的新语法、关联数组或 PS0。
#
# 命令边界：
# - precmd 放在 PROMPT_COMMAND 最前，首句取 $?，有命令在执行时发 command_returned；
# - arm 放在 PROMPT_COMMAND 最后，此后到达的第一个 DEBUG trap 才是用户命令，
#   PROMPT_COMMAND 里用户自己的钩子因此不会被误判为命令；
# - shell_prompt 挂在 PS1 开头由 readline 输出：readline 先把终端切到非回显模式再画提示符，
#   标记到达时输入已可安全写入。若在 PROMPT_COMMAND 里直接打印，标记早于 readline 就绪，
#   紧随写入的行会先被 tty 规范模式回显一次、再被 readline 重画一次。
# - 空回车不执行任何命令，没有 DEBUG，也就没有 started/returned，只有新的 shell_prompt。

__cosir_active=0
__cosir_armed=0
__cosir_hist_seen=
__cosir_hist_ready=
__cosir_prompt_marker=
__cosir_prompt_seq=0
__cosir_user_ps1=
__cosir_wrapped_ps1=

__cosir_escape() {
  local value bs='\'
  # 赋值上下文 + 引号包住替换串：bash 3.2 与 5.2+（patsub_replacement）对反斜杠的处理才一致。
  value=${1//"$bs"/"$bs$bs"}
  value=${value//;/"${bs}x3b"}
  value=${value//$'\n'/"${bs}x0a"}
  value=${value//$'\r'/"${bs}x0d"}
  value=${value//$'\033'/"${bs}x1b"}
  value=${value//$'\a'/"${bs}x07"}
  __cosir_reply=$value
}

__cosir_emit() {
  builtin printf '\033]633;Cosir;%s;%s;%s\007' "$__cosir_token" "$1" "$2"
}

# 命令文本优先取 history 1（完整命令行，含管道与多行）；该条未进历史
#（ignorespace、ignoredups、history 关闭）时历史号不变，退回 BASH_COMMAND（仅首个简单命令）。
__cosir_command_text() {
  local line number pattern='^ *([0-9]+)[* ] (.*)$'
  line=$(HISTTIMEFORMAT= builtin history 1)
  if [[ $line =~ $pattern ]]; then
    number=${BASH_REMATCH[1]}
    if [ "$number" != "$__cosir_hist_seen" ]; then
      __cosir_hist_seen=$number
      __cosir_reply=${BASH_REMATCH[2]}
      return
    fi
  fi
  __cosir_reply=$1
}

__cosir_preexec() {
  local status=$? command=$BASH_COMMAND
  # 跳过：补全（COMP_LINE）、集成自身函数；用户开了 functrace（set -T）时
  # DEBUG 也会在集成函数内部触发（FUNCNAME[1] 为集成函数），同样不是用户命令。
  case "${FUNCNAME[1]-}" in __cosir_*) return $status ;; esac
  if [ "$__cosir_armed" = 1 ] && [ -z "${COMP_LINE-}" ]; then
    case "$command" in
      __cosir_*) ;;
      *)
        __cosir_armed=0
        __cosir_active=1
        __cosir_command_text "$command"
        __cosir_escape "${__cosir_reply:0:2048}"
        __cosir_emit command_started "$__cosir_reply"
        ;;
    esac
  fi
  # 交还 $? 给链上的用户 DEBUG trap。
  return $status
}

__cosir_precmd() {
  local status=$?
  __cosir_armed=0
  # 历史文件在启动文件之后才载入，故在首个提示符前取历史号基线：
  # 首条命令即使未进历史，也不会误取上次会话留下的旧命令。
  if [ -z "$__cosir_hist_ready" ]; then
    __cosir_hist_ready=1
    __cosir_command_text ""
  fi
  if [ "$__cosir_active" = 1 ]; then
    __cosir_emit command_returned "$status"
    __cosir_active=0
  fi
  # 先还原上次包装的 PS1，用户钩子看到的始终是自己的 PS1；
  # 用户在命令里改过 PS1（与包装值不同）时保留用户的新值。
  if [ -n "$__cosir_wrapped_ps1" ] && [ "${PS1-}" = "$__cosir_wrapped_ps1" ]; then
    PS1=$__cosir_user_ps1
  fi
  # 编号随每次 PROMPT_COMMAND 递增：readline 重画提示符会重复输出同一标记，
  # worker 按编号去重；空回车是新编号，仍报告新的 shell_prompt。
  __cosir_prompt_seq=$((__cosir_prompt_seq + 1))
  __cosir_escape "$PWD"
  __cosir_prompt_marker=$'\033]633;Cosir;'"$__cosir_token;shell_prompt;$__cosir_reply;$__cosir_prompt_seq"$'\007'
  # PROMPT_COMMAND 后续的用户钩子与 PS1 仍能读到上一条命令的退出码。
  return $status
}

__cosir_arm() {
  local status=$?
  if shopt -q promptvars; then
    # 以变量引用挂入：PS1 先解码反斜杠再做参数展开，展开结果不会被二次解码，
    # cwd 里的 \、$ 因此原样输出；\[ \] 让 readline 不把标记计入提示符宽度。
    __cosir_user_ps1=${PS1-}
    PS1='\[$__cosir_prompt_marker\]'"${PS1-}"
    __cosir_wrapped_ps1=$PS1
  else
    # 用户关闭了 promptvars 时无法安全挂入 PS1，退回立即打印（就绪前写入会多一次回显）。
    __cosir_wrapped_ps1=
    builtin printf '%s' "$__cosir_prompt_marker"
  fi
  __cosir_armed=1
  return $status
}

__cosir_capture_trap() { __cosir_user_debug=$2; }
