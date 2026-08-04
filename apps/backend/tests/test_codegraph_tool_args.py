"""CodeGraph 工具参数模型校验测试。

覆盖 6 个参数模型的 pydantic 校验：必填、默认值、类型、extra=forbid。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.tools.tool_models import (
    CodegraphCalleesArgs,
    CodegraphCallersArgs,
    CodegraphExploreArgs,
    CodegraphImpactArgs,
    CodegraphNodeArgs,
    CodegraphSearchArgs,
)


def test_explore_args_required_and_defaults():
    args = CodegraphExploreArgs(query="AuthService loginUser")
    assert args.query == "AuthService loginUser"
    assert args.max_files == 12


def test_explore_args_rejects_extra():
    with pytest.raises(ValidationError):
        CodegraphExploreArgs(query="x", bogus=1)


def test_search_args_defaults():
    args = CodegraphSearchArgs(query="signIn")
    assert args.limit == 10
    assert args.kind is None


def test_search_args_kind_validation():
    with pytest.raises(ValidationError):
        CodegraphSearchArgs(query="x", kind="bogus_kind")


def test_node_args_symbol_mode():
    args = CodegraphNodeArgs(symbol="foo", include_code=True)
    assert args.symbol == "foo"
    assert args.include_code is True


def test_node_args_file_mode():
    args = CodegraphNodeArgs(file="src/auth/session.ts", symbols_only=True)
    assert args.file == "src/auth/session.ts"
    assert args.symbols_only is True


def test_callers_args_required_symbol():
    args = CodegraphCallersArgs(symbol="UserService")
    assert args.symbol == "UserService"
    assert args.limit == 20


def test_callers_args_missing_symbol_raises():
    with pytest.raises(ValidationError):
        CodegraphCallersArgs()  # type: ignore[call-arg]


def test_callees_args_defaults():
    args = CodegraphCalleesArgs(symbol="parse")
    assert args.limit == 20


def test_impact_args_defaults():
    args = CodegraphImpactArgs(symbol="refactor_target")
    assert args.depth == 2


def test_impact_args_depth_range():
    with pytest.raises(ValidationError):
        CodegraphImpactArgs(symbol="x", depth=0)
