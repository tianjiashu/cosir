#!/usr/bin/env bash
# 新增文件强类型门禁脚本（供 .pre-commit-config.yaml 的 mypy-new-strict hook 调用）。
#
# 目标：本次提交中「新增」的 apps/backend/app/ 下 .py 文件（git diff --cached
# --diff-filter=A）必须通过 mypy --strict（mypy.strict.ini，不含存量豁免），
# 未通过则非零退出、阻断 git commit。存量/修改文件不受影响，仍由
# mypy_nonblocking.sh 非阻塞提示。
#
# 为什么用独立 mypy.strict.ini：pyproject.toml 的 [[tool.mypy.overrides]]
# module = ["app.*"] 会豁免 app/ 下所有模块（含新文件），故此处改用无豁免配置，
# 使「新代码强类型」成为工具强制而非人工纪律。
#
# 用法：由 pre-commit 以 `bash apps/backend/scripts/mypy_new_strict.sh` 调用（在仓库根执行）。

set -euo pipefail

# 仓库根（脚本位于 apps/backend/scripts/，上溯三级）
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$repo_root"

# 本次暂存的新增 .py（限定 apps/backend/app/ 业务代码；tests/temp 不在此门禁内）。
# 必须用 :(glob) magic：普通 pathspec 的 * 不跨目录，会漏掉 app/ 嵌套新增文件
# （如 app/core/xxx.py），使门禁形同虚设。
new_py_files="$(git diff --cached --name-only --diff-filter=A -- ':(glob)apps/backend/app/**/*.py' || true)"
if [[ -z "$new_py_files" ]]; then
  exit 0
fi

echo "mypy --strict 检查新增文件（强类型门禁）："
echo "$new_py_files"

# 逐行读入数组并去除可能的 CR 残留（Windows checkout 下 git 输出可能带 CRLF），
# 避免含空格路径被按词拆分导致门禁漏检或误判。
mapfile -t new_file_list <<< "$new_py_files"
new_file_list=("${new_file_list[@]%$'\r'}")
uv run --project apps/backend mypy --config-file apps/backend/mypy.strict.ini "${new_file_list[@]}"
