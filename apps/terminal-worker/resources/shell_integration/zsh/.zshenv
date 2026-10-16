# 保留用户启动链路；仅本进程的后续启动文件经过临时目录。
typeset -g __cosir_boot_dir="$ZDOTDIR"
typeset -g __cosir_user_dir="$COSIR_USER_ZDOTDIR"
ZDOTDIR="$__cosir_user_dir"
[[ -f "$ZDOTDIR/.zshenv" ]] && source "$ZDOTDIR/.zshenv"
__cosir_user_dir="$ZDOTDIR"
ZDOTDIR="$__cosir_boot_dir"
