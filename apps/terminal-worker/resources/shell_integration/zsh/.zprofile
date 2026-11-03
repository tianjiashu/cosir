# login shell 才会读到这里（macOS）；用户 .zshenv 可能改了 ZDOTDIR，按它找用户的 .zprofile。
ZDOTDIR="$__cosir_user_dir"
[[ -f "$ZDOTDIR/.zprofile" ]] && source "$ZDOTDIR/.zprofile"
__cosir_user_dir="$ZDOTDIR"
ZDOTDIR="$__cosir_boot_dir"
