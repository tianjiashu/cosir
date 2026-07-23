#!/usr/bin/env bash
# 渐进式类型检查包装脚本（供 ``.pre-commit-config.yaml`` 的 mypy hook 调用）。
#
# 背景：pre-commit 4.x 已移除 ``continue-on-error`` 配置键，无法直接声明
# 「hook 失败不阻断提交」。而 AGENTS.md 约定 mypy 为「渐进式先非阻塞」，存量
# 未注解代码不应堵死提交。
#
# 做法：本脚本运行 mypy 并完整输出结果，但始终以退出码 0 返回；配合 mypy hook
# 的 ``verbose: true``，pre-commit 仍会把 mypy 结果打印出来，同时不阻断 git commit。
#
# 待存量类型注解逐步补全、mypy 可干净通过之后，可移除本包装、直接强制 mypy 通过。

uv run --project apps/backend mypy --config-file apps/backend/pyproject.toml "$@"
exit 0
