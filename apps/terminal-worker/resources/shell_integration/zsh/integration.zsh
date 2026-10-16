# 集成只观察根 shell；输入仍由 ZLE 与前台程序处理。
autoload -Uz add-zsh-hook
typeset -g __cosir_token="$COSIR_INTEGRATION_TOKEN"
unset COSIR_INTEGRATION_TOKEN COSIR_USER_ZDOTDIR
typeset -g __cosir_active=0
function __cosir_escape() {
  emulate -L zsh
  REPLY="${1//\\/\\\\}"
  REPLY="${REPLY//;/\\x3b}"
  REPLY="${REPLY//$'\n'/\\x0a}"
  REPLY="${REPLY//$'\r'/\\x0d}"
  REPLY="${REPLY//$'\e'/\\x1b}"
  REPLY="${REPLY//$'\a'/\\x07}"
}
function __cosir_preexec() {
  __cosir_active=1
  local REPLY
  __cosir_escape "${1[1,2048]}"
  builtin printf '\e]633;Cosir;%s;command_started;%s\a' "$__cosir_token" "$REPLY"
}
function __cosir_precmd() {
  local result=$?
  if (( __cosir_active )); then
    builtin printf '\e]633;Cosir;%s;command_returned;%s\a' "$__cosir_token" "$result"
    __cosir_active=0
  fi
  local REPLY
  __cosir_escape "$PWD"
  builtin printf '\e]633;Cosir;%s;shell_prompt;%s\a' "$__cosir_token" "$REPLY"
}
add-zsh-hook preexec __cosir_preexec
add-zsh-hook precmd __cosir_precmd
builtin printf '\e]633;Cosir;%s;ready;zsh;%s\a' "$__cosir_token" "$ZSH_VERSION"
