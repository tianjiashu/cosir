"""``.cosir`` 路径集中与只读保护改造 —— 第二轮（修复后）独立对抗性回归验证。

只测不改：不修改 ``apps/backend/app/`` 下任何生产代码。

本轮针对开发者收到上一轮独立审查意见后的**修复性改动**做回归：

1. ``attachment_service`` 新增 ``_TaskAttachmentDirs`` 冻结 dataclass 与 ``_dirs(task_id)``
   （替换旧 ``_directory``）；``workspace_root`` 直接取自 workspace 记录，不再用
   ``directory.parent.parent`` 反推。7 个调用点全部迁移。
2. ``is_within_cosir`` 新增空白路径短路：``""`` / ``"   "`` / ``Path("")`` 必须返回 ``False``。
3. ``tool_output_budget._write_artifact`` 统一使用已 resolve 的 ``root`` 构造 PathResolver 与
   ``atomicWrite containment_root``。
4. ``image_utils.is_trusted_cosir_path`` docstring 补异常段（无行为变更）。

对抗维度：
  A. 附件功能回归：dedup / normalizer / runtime-context 全绿（另由既有文件覆盖）；本文件额外
     构造 resolve_for_model、relative_path、upload staging 的显式用例。
  B. 等价性：修复后 ``relative_path`` / ``resolve_for_model`` 基准 == 修复前
     ``directory.parent.parent``（含 workspace 根为符号链接场景）。
  C. artifact 落盘位置、回读原文、符号链接 workspace 根的 containment。
  D. ``is_within_cosir`` 空白短路 + 正常路径判定不被破坏（复跑上一轮对抗用例）。
  E. 五个文件工具指向 ``.cosir`` 仍全部失败且磁盘不变；只读工具仍可读；终端 cwd 仍豁免。
"""

from __future__ import annotations

import contextlib
import hashlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from starlette.datastructures import UploadFile

from app.core.tools.guard.tool_output_budget import ToolOutputBudget
from app.core.tools.schemas import ToolExecutionContext, ToolObservation
from app.core.tools.tool_handler.apply_patch_tool import ApplyPatchTool
from app.core.tools.tool_handler.delete_tool import DeleteTool
from app.core.tools.tool_handler.find_files import FindFilesTool
from app.core.tools.tool_handler.list_directory import ListDirectoryTool
from app.core.tools.tool_handler.move_tool import MoveTool
from app.core.tools.tool_handler.read_file import ReadFileTool
from app.core.tools.tool_handler.replace_tool import ReplaceTool
from app.core.tools.tool_handler.search_content import SearchContentTool
from app.core.tools.tool_handler.write_file import WriteFileTool
from app.service.attachment.attachment_service import AttachmentService
from app.service.attachment.image_normalizer import ImageNormalizationError
from app.service.terminal.terminal_session_service import TerminalSessionService
from app.utils import cosir_paths

_VISION_MODEL = "deepseek-flash"
_ASSET = "a" * 64


def _ctx(root: Path) -> ToolExecutionContext:
    """构造绑定到给定 workspace 根的执行上下文。"""

    return ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=root, run_id=1)


def _png_bytes(size: tuple[int, int] = (4, 3)) -> bytes:
    """生成一张确定性的 PNG 字节流。"""

    stream = BytesIO()
    Image.new("RGB", size, (20, 40, 60)).save(stream, format="PNG")
    return stream.getvalue()


def _upload(data: bytes, name: str = "image.png") -> UploadFile:
    """构造一个附件上传对象。"""

    return UploadFile(file=BytesIO(data), filename=name, headers=None)


def _service(
    root_path: str | Path, *, workspace_id: int = 7, task_id: int = 7
) -> AttachmentService:
    """构造仅注入轻量桩的 ``AttachmentService``（不触网、不落库）。"""

    service = AttachmentService.__new__(AttachmentService)
    service._tasks = SimpleNamespace(
        get_task=lambda _task_id: SimpleNamespace(workspace_id=workspace_id, id=task_id),
        list_tasks_for_workspace=lambda _workspace_id: [],
    )
    service._workspaces = SimpleNamespace(
        get_workspace=lambda _workspace_id: SimpleNamespace(root_path=str(root_path))
    )
    service._runs = SimpleNamespace(list_runs_for_task=lambda _task_id: [])
    return service


def _symlinks_supported(base: Path) -> bool:
    """探测当前环境是否允许创建符号链接（Windows 需开发者模式/管理员）。"""

    target = base / "_probe_target.txt"
    target.write_text("x", encoding="utf-8")
    link = base / "_probe_link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        return False
    finally:
        for candidate in (link, target):
            with contextlib.suppress(OSError):
                candidate.unlink()
    return True


# --- A. 附件功能回归：resolve_for_model / relative_path / upload staging ------------------


async def test_resolve_for_model_resolves_run_referenced_cosir_attachment(tmp_path: Path) -> None:
    """run 引用的 ``.cosir/Attachment/<sha256>.png`` 能被 resolve_for_model 正确解析并归一化。

    潜在缺陷类型：修复把 ``root`` 从 ``directory.parent.parent`` 改为 workspace 记录后，
    基准若不一致会导致 run 引用解析失败（附件视觉通道整体回归）。
    """

    service = _service(tmp_path)
    data = _png_bytes()
    asset_id = hashlib.sha256(data).hexdigest()
    uploaded = await service.upload(7, _upload(data))
    assert uploaded.asset_id == asset_id
    # upload 仅把字节落到 .uploading 暂存；finalize 才把文件落进 Attachment/。
    service.finalize(7, asset_id, _VISION_MODEL)

    # 模拟 Run 引用：workspace 相对的 .cosir/Attachment/<sha>.png
    normalized = service.resolve_for_model(7, f".cosir/Attachment/{asset_id}.png", _VISION_MODEL)

    assert normalized.path.is_file()
    assert normalized.path.parent == (tmp_path / ".cosir" / "Attachment").resolve()
    assert normalized.path.name == f"{asset_id}.png"
    assert normalized.target_format == "png"
    assert normalized.content_type == "image/png"


def test_relative_path_returns_cosir_attachment_workspace_relative(tmp_path: Path) -> None:
    """``relative_path`` 必须返回 ``.cosir/Attachment/<file>`` 形式的 workspace 相对路径。

    潜在缺陷类型：基准从 ``directory.parent.parent`` 改为 workspace 记录后若多/少一层，
    返回路径会变成 ``Attachment/<file>`` 或含 ``.cosir`` 前缀重复。
    """

    service = _service(tmp_path)
    attachment_dir = tmp_path / ".cosir" / "Attachment"
    attachment_dir.mkdir(parents=True)
    target = attachment_dir / f"{_ASSET}.png"
    target.write_bytes(b"x")

    result = service.relative_path(7, target)

    assert result == f".cosir/Attachment/{_ASSET}.png"
    assert not Path(result).is_absolute()
    assert "\\" not in result


async def test_upload_staging_directory_is_cosir_uploading(tmp_path: Path) -> None:
    """``upload`` 的 staging 目录必须仍是 ``<root>/.cosir/Attachment/.uploading``。

    潜在缺陷类型：staging 目录改为 workspace 记录基准后若拼错层级，会写到别处或新建额外目录。
    """

    service = _service(tmp_path)
    data = _png_bytes()
    asset_id = hashlib.sha256(data).hexdigest()

    await service.upload(7, _upload(data))

    staging = tmp_path / ".cosir" / "Attachment" / ".uploading"
    staged = staging / f"{asset_id}.png"
    assert staged.is_file()
    assert staged.read_bytes() == data
    # 不得在工作区根直接留下散落的 staging 目录。
    assert not (tmp_path / ".uploading").exists()


def test_relative_path_rejects_path_outside_attachment_dir(tmp_path: Path) -> None:
    """``relative_path`` 对附件目录之外的路径必须拒绝（ATTACHMENT_NOT_OWNED）。

    潜在缺陷类型：``_inside`` 基准调整后 containment 判断被放宽，越界路径被换算成相对路径。
    """

    service = _service(tmp_path)
    (tmp_path / ".cosir" / "Attachment").mkdir(parents=True)
    outside = tmp_path / "secret.txt"
    outside.write_text("x", encoding="utf-8")

    with pytest.raises(ImageNormalizationError) as error:
        service.relative_path(7, outside)

    assert error.value.code == "ATTACHMENT_NOT_OWNED"


def test_resolve_for_model_rejects_path_outside_attachment(tmp_path: Path) -> None:
    """``resolve_for_model`` 对 workspace 内但非 ``.cosir/Attachment`` 的引用必须拒绝。

    潜在缺陷类型：基准改为 workspace 记录后，``_inside`` 只校验「在附件目录内」，
    若候选父目录判断失效，工作区其它路径会被误当作附件。
    """

    service = _service(tmp_path)
    (tmp_path / ".cosir" / "Attachment").mkdir(parents=True)
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")

    with pytest.raises(ImageNormalizationError) as error:
        service.resolve_for_model(7, "notes.txt", _VISION_MODEL)

    # 非附件目录内的路径由 _inside 的 containment 拒绝（越界先于存在性判定）。
    assert error.value.code == "ATTACHMENT_NOT_OWNED"


def test_resolve_for_model_rejects_traversal_escape(tmp_path: Path) -> None:
    """``resolve_for_model`` 对借 ``..`` 逃出附件目录的引用必须拒绝。

    潜在缺陷类型：改用 workspace_root / image_path 拼接后，未 ``resolve`` 的 `..` 逃逸可能绕过
    ``_inside``。
    """

    service = _service(tmp_path)
    (tmp_path / ".cosir" / "Attachment").mkdir(parents=True)

    with pytest.raises(ImageNormalizationError):
        service.resolve_for_model(
            7, f".cosir/Attachment/../../../{_ASSET}.png", _VISION_MODEL
        )


# --- B. 等价性：修复前 directory.parent.parent == 修复后 workspace 记录基准 --------------


def test_relative_path_baseline_equivalent_when_root_is_symlink(tmp_path: Path) -> None:
    """workspace 根为符号链接时，``relative_path`` 基准与修复前 ``directory.parent.parent`` 等价。

    修复前：``directory = Path(root_path).resolve()/.cosir/Attachment``，
    ``directory.parent.parent == Path(root_path).resolve()``。
    修复后：``workspace_root = Path(workspace.root_path).resolve()``。
    两者必须对同一文件产出**完全一致**的相对路径。
    """

    if not _symlinks_supported(tmp_path):
        pytest.skip("当前环境不支持创建符号链接")
    real_root = tmp_path / "real_ws"
    real_root.mkdir()
    link_root = tmp_path / "link_ws"
    link_root.symlink_to(real_root, target_is_directory=True)

    attachment_dir = real_root / ".cosir" / "Attachment"
    attachment_dir.mkdir(parents=True)
    target = attachment_dir / f"{_ASSET}.png"
    target.write_bytes(b"x")

    service = _service(link_root)
    result = service.relative_path(7, target)

    expected = target.relative_to(real_root).as_posix()
    assert result == expected == f".cosir/Attachment/{_ASSET}.png"


def test_resolve_for_model_equivalent_when_root_is_symlink(tmp_path: Path) -> None:
    """workspace 根为符号链接时，``resolve_for_model`` 基准与修复前等价且能解析 run 引用。"""

    if not _symlinks_supported(tmp_path):
        pytest.skip("当前环境不支持创建符号链接")
    real_root = tmp_path / "real_ws"
    real_root.mkdir()
    link_root = tmp_path / "link_ws"
    link_root.symlink_to(real_root, target_is_directory=True)

    attachment_dir = real_root / ".cosir" / "Attachment"
    attachment_dir.mkdir(parents=True)
    data = _png_bytes()
    asset_id = hashlib.sha256(data).hexdigest()
    (attachment_dir / f"{asset_id}.png").write_bytes(data)

    service = _service(link_root)
    normalized = service.resolve_for_model(
        7, f".cosir/Attachment/{asset_id}.png", _VISION_MODEL
    )

    assert normalized.path.is_file()
    assert normalized.path.read_bytes() == data


def test_relative_path_symlink_root_matches_lexical_double_parent(tmp_path: Path) -> None:
    """显式对比：修复后基准 == 修复前 ``directory.parent.parent``（符号链接根场景逐项相等）。"""

    if not _symlinks_supported(tmp_path):
        pytest.skip("当前环境不支持创建符号链接")
    real_root = tmp_path / "real_ws"
    real_root.mkdir()
    link_root = tmp_path / "link_ws"
    link_root.symlink_to(real_root, target_is_directory=True)

    attachment_dir = real_root / ".cosir" / "Attachment"
    attachment_dir.mkdir(parents=True)
    target = attachment_dir / f"{_ASSET}.png"
    target.write_bytes(b"x")

    service = _service(link_root)
    new_baseline = service._dirs(7).workspace_root  # type: ignore[attr-defined]
    old_baseline = (Path(str(link_root)).resolve() / ".cosir" / "Attachment").parent.parent
    assert new_baseline == old_baseline


# --- C. artifact 落盘 ----------------------------------------------------------------


def test_artifact_still_lands_under_cosir_and_reads_back(tmp_path: Path) -> None:
    """超限输出仍在 ``<root>/.cosir/tool-artifacts/`` 落盘并可回读原文（修复后回归）。"""

    root = tmp_path / "ws"
    root.mkdir()
    budget = ToolOutputBudget(max_chars=10)
    content = "abcdefghij" * 100
    obs = ToolObservation(tool_name="t", status="success", content=content)

    result = budget.apply(obs, _ctx(root))

    artifact_path = (result.artifact_data or {}).get("artifact_path")
    assert isinstance(artifact_path, str) and artifact_path
    assert artifact_path.startswith(".cosir/tool-artifacts/")
    on_disk = root / artifact_path
    assert on_disk.is_file()
    assert on_disk.read_text(encoding="utf-8") == content


def test_artifact_containment_when_root_is_symlink(tmp_path: Path) -> None:
    """workspace 根为符号链接时，artifact 必须落真实目标内且可回读（containment 仍成立）。"""

    if not _symlinks_supported(tmp_path):
        pytest.skip("当前环境不支持创建符号链接")
    real_root = tmp_path / "real_ws"
    real_root.mkdir()
    link_root = tmp_path / "link_ws"
    link_root.symlink_to(real_root, target_is_directory=True)

    budget = ToolOutputBudget(max_chars=5)
    content = "y" * 500
    obs = ToolObservation(tool_name="t", status="success", content=content)
    result = budget.apply(obs, _ctx(link_root))

    artifact_path = (result.artifact_data or {}).get("artifact_path")
    assert isinstance(artifact_path, str) and artifact_path.startswith(".cosir/tool-artifacts/")
    written = real_root / Path(artifact_path)
    assert written.is_file()
    assert written.read_text(encoding="utf-8") == content


def test_artifact_not_written_outside_when_cosir_is_outside_symlink(tmp_path: Path) -> None:
    """``.cosir`` 指向 workspace 外时，artifact 不得越界写到外部真实目录。"""

    if not _symlinks_supported(tmp_path):
        pytest.skip("当前环境不支持创建符号链接")
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".cosir").symlink_to(outside, target_is_directory=True)

    budget = ToolOutputBudget(max_chars=5)
    obs = ToolObservation(tool_name="t", status="success", content="z" * 500)
    result = budget.apply(obs, _ctx(root))
    artifact_path = (result.artifact_data or {}).get("artifact_path")
    if artifact_path:
        written = (root / artifact_path).resolve()
        assert written.is_relative_to(root.resolve()), f"artifact 越界：{written}"


# --- D. is_within_cosir 空白短路 --------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n", Path("")])
def test_is_within_cosir_blank_path_returns_false(tmp_path: Path, blank: object) -> None:
    """空白路径（``""`` / 纯空白 / ``Path("")``）必须短路返回 ``False``。

    潜在缺陷类型：``realpath("")`` 可能落到当前工作目录，导致误判为在 ``.cosir`` 内。
    """

    root = tmp_path / "ws"
    (root / ".cosir").mkdir(parents=True)
    assert cosir_paths.is_within_cosir(blank, root) is False


def test_is_within_cosir_blank_does_not_depend_on_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """即使进程 cwd 切到 ``.cosir`` 内，空白路径仍必须返回 ``False``（不落 cwd 语义）。"""

    root = tmp_path / "ws"
    cosir = root / ".cosir"
    cosir.mkdir(parents=True)
    monkeypatch.chdir(cosir)

    assert cosir_paths.is_within_cosir("", root) is False
    assert cosir_paths.is_within_cosir("   ", root) is False


def test_is_within_cosir_normal_paths_unaffected_by_blank_short_circuit(tmp_path: Path) -> None:
    """空白短路不得影响正常路径判定（复跑上一轮对抗用例）。"""

    root = tmp_path / "ws"
    (root / ".cosir").mkdir(parents=True)
    (root / ".cosir2").mkdir(parents=True)

    # 命中
    assert cosir_paths.is_within_cosir(root / ".cosir", root) is True
    assert cosir_paths.is_within_cosir(root / ".cosir" / "x.txt", root) is True
    assert cosir_paths.is_within_cosir(root / "./.cosir" / "x", root) is True
    assert cosir_paths.is_within_cosir(root / ".cosir" / "x" / ".." / "y", root) is True
    # 不命中
    assert cosir_paths.is_within_cosir(root / ".cosir2" / "x", root) is False
    assert cosir_paths.is_within_cosir(root / "src" / "x.txt", root) is False
    # workspace_root 无效
    assert cosir_paths.is_within_cosir(root / ".cosir" / "x", None) is False
    assert cosir_paths.is_within_cosir(root / ".cosir" / "x", "") is False
    assert cosir_paths.is_within_cosir(root / ".cosir" / "x", "   ") is False


def test_is_trusted_cosir_path_blank_is_false(tmp_path: Path) -> None:
    """``is_trusted_cosir_path`` 直接暴露空白短路行为（委托 is_within_cosir）。"""

    from app.utils.image_utils import is_trusted_cosir_path

    root = tmp_path / "ws"
    (root / ".cosir").mkdir(parents=True)
    assert is_trusted_cosir_path("", str(root)) is False
    assert is_trusted_cosir_path("   ", str(root)) is False
    assert is_trusted_cosir_path(str(root / ".cosir" / "a.png"), str(root)) is True


# --- E. 端到端：五个文件工具 + 只读工具 + 终端豁免 ---------------------------------------


def test_write_file_into_cosir_no_disk_change(tmp_path: Path) -> None:
    """write_file 指向 ``.cosir`` 子文件：error 且磁盘不变。"""

    root = tmp_path / "ws"
    (root / ".cosir").mkdir(parents=True)
    obs = WriteFileTool().execute(path=".cosir/new.txt", content="hi", execution_context=_ctx(root))
    assert obs.status == "error"
    assert not (root / ".cosir" / "new.txt").exists()


def test_patch_write_into_cosir_no_disk_change(tmp_path: Path) -> None:
    """patch_write 指向 ``.cosir`` 已有文件：error 且内容不变。"""

    root = tmp_path / "ws"
    target = root / ".cosir" / "a.txt"
    target.parent.mkdir(parents=True)
    target.write_text("hello", encoding="utf-8")
    obs = ReplaceTool().execute(
        execution_context=_ctx(root), path=".cosir/a.txt", old_string="hello", new_string="bye"
    )
    assert obs.status == "error"
    assert target.read_text(encoding="utf-8") == "hello"


def test_apply_patch_into_cosir_no_disk_change(tmp_path: Path) -> None:
    """apply_patch 指向 ``.cosir`` 已有文件：error 且内容不变。"""

    root = tmp_path / "ws"
    target = root / ".cosir" / "a.txt"
    target.parent.mkdir(parents=True)
    target.write_text("hello\n", encoding="utf-8")
    patch = (
        "diff --git a/.cosir/a.txt b/.cosir/a.txt\n"
        "--- a/.cosir/a.txt\n"
        "+++ b/.cosir/a.txt\n"
        "@@ -1 +1 @@\n"
        "-hello\n"
        "+bye\n"
    )
    obs = ApplyPatchTool().execute(execution_context=_ctx(root), patch=patch)
    assert obs.status == "error"
    assert target.read_text(encoding="utf-8") == "hello\n"


def test_delete_file_in_cosir_no_disk_change(tmp_path: Path) -> None:
    """delete_file 指向 ``.cosir`` 子文件：error 且文件存活。"""

    root = tmp_path / "ws"
    target = root / ".cosir" / "Attachment" / "a.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    obs = DeleteTool().execute(execution_context=_ctx(root), path=".cosir/Attachment/a.png")
    assert obs.status == "error"
    assert target.exists()


def test_move_file_out_of_cosir_no_disk_change(tmp_path: Path) -> None:
    """move_file 源在 ``.cosir``：error 且文件存活。"""

    root = tmp_path / "ws"
    source = root / ".cosir" / "src.txt"
    source.parent.mkdir(parents=True)
    source.write_text("a", encoding="utf-8")
    obs = MoveTool().execute(
        execution_context=_ctx(root), source_path=".cosir/src.txt", destination_path="src.txt"
    )
    assert obs.status == "error"
    assert source.exists()


def test_read_file_reads_cosir(tmp_path: Path) -> None:
    """read_file 读 ``.cosir`` 内文件成功（只读工具仍可读）。"""

    root = tmp_path / "ws"
    target = root / ".cosir" / "a.txt"
    target.parent.mkdir(parents=True)
    target.write_text("secret-content", encoding="utf-8")
    obs = ReadFileTool().execute(path=".cosir/a.txt", execution_context=_ctx(root))
    assert obs.status == "success"


def test_list_directory_lists_cosir(tmp_path: Path) -> None:
    """list_directory 列 ``.cosir`` 成功。"""

    root = tmp_path / "ws"
    target = root / ".cosir" / "a.txt"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    obs = ListDirectoryTool().execute(
        path=".cosir", execution_context=_ctx(root), include_hidden=True
    )
    assert obs.status == "success"


def test_search_content_searches_cosir(tmp_path: Path) -> None:
    """search_content 搜 ``.cosir`` 成功。"""

    root = tmp_path / "ws"
    target = root / ".cosir" / "a.txt"
    target.parent.mkdir(parents=True)
    target.write_text("needle-here", encoding="utf-8")
    obs = SearchContentTool().execute(pattern="needle", path=".cosir", execution_context=_ctx(root))
    assert obs.status == "success"


def test_find_files_searches_cosir(tmp_path: Path) -> None:
    """find_files 在 ``.cosir`` 下查找成功。"""

    root = tmp_path / "ws"
    target = root / ".cosir" / "a.txt"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    obs = FindFilesTool().execute(pattern="*.txt", path=".cosir", execution_context=_ctx(root))
    assert obs.status == "success"


def test_terminal_cwd_cosir_allowed(tmp_path: Path) -> None:
    """终端 cwd 落在 ``.cosir`` 仍豁免。"""

    root = tmp_path / "ws"
    cosir = root / ".cosir"
    cosir.mkdir(parents=True)
    assert TerminalSessionService._resolve_cwd(str(root), ".cosir") == cosir


# --- F. 附加对抗：resolve_for_model 与 relative_path 往返一致性 --------------------------


async def test_roundtrip_upload_relative_path_then_resolve(tmp_path: Path) -> None:
    """upload -> relative_path -> resolve_for_model 往返必须闭环（修复后路径基准自洽）。

    潜在缺陷类型：relative_path 与 resolve_for_model 若使用不同基准，往返会失败。
    """

    service = _service(tmp_path)
    data = _png_bytes()
    await service.upload(7, _upload(data))

    # 用户发送消息时 finalize 得到 workspace 相对路径（模拟 conversation_run_service 行为）
    asset_id = hashlib.sha256(data).hexdigest()
    finalized = service.finalize(7, asset_id, _VISION_MODEL)
    rel = service.relative_path(7, finalized.path)
    assert rel == f".cosir/Attachment/{asset_id}.png"

    # 运行期按该相对路径解析回图片
    normalized = service.resolve_for_model(7, rel, _VISION_MODEL)
    again = service.resolve_for_model(7, rel, _VISION_MODEL)
    assert normalized.path.read_bytes() == again.path.read_bytes() == data
    assert normalized.width == 4 and normalized.height == 3


def test_artifact_path_text_matches_os_separator_handling(tmp_path: Path) -> None:
    """artifact 返回路径必须是 POSIX 形式（不含反斜杠），与磁盘布局一致。"""

    root = tmp_path / "ws"
    root.mkdir()
    budget = ToolOutputBudget(max_chars=5)
    obs = ToolObservation(tool_name="t", status="success", content="x" * 500)
    result = budget.apply(obs, _ctx(root))
    artifact_path = (result.artifact_data or {}).get("artifact_path")
    assert "\\" not in str(artifact_path)
    assert (root / artifact_path).is_file()


# --- G. _dirs 冻结 dataclass 契约与异常传播 ---------------------------------------------


def test_dirs_returns_frozen_workspace_root_and_attachment_dir(tmp_path: Path) -> None:
    """``_dirs`` 必须返回 workspace 记录解析根 + ``<root>/.cosir/Attachment``，且不可变。

    潜在缺陷类型：``_TaskAttachmentDirs`` 误标 mutable，或实现里 workspace_root 与
    attachment_dir 取自不同基准导致二者不自洽。
    """

    service = _service(tmp_path)
    dirs = service._dirs(7)  # type: ignore[attr-defined]

    from dataclasses import FrozenInstanceError

    assert dirs.workspace_root == tmp_path.resolve()
    assert dirs.attachment_dir == tmp_path.resolve() / ".cosir" / "Attachment"
    assert dirs.attachment_dir.parent.parent == dirs.workspace_root
    with pytest.raises(FrozenInstanceError):
        dirs.workspace_root = tmp_path  # type: ignore[misc]


def test_dirs_propagates_missing_task_keyerror(tmp_path: Path) -> None:
    """task 不存在时 ``_dirs`` 必须让 KeyError 透出（docstring 声明），不得吞掉。"""

    service = _service(tmp_path)

    def _missing(_task_id: int) -> object:
        raise KeyError(_task_id)

    service._tasks.get_task = _missing  # type: ignore[assignment]

    with pytest.raises(KeyError):
        service._dirs(999)  # type: ignore[attr-defined]


def test_dirs_rejects_attachment_dir_symlink(tmp_path: Path) -> None:
    """``_dirs`` 在 ``.cosir/Attachment`` 被做成符号链接时必须抛 ATTACHMENT_STORAGE_UNAVAILABLE。

    潜在缺陷类型：改用 workspace 记录 + cosir_paths 拼接后，reparse point 检查被绕过。
    """

    if not _symlinks_supported(tmp_path):
        pytest.skip("当前环境不支持创建符号链接")
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".cosir").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".cosir" / "Attachment").symlink_to(outside, target_is_directory=True)

    service = _service(root)

    with pytest.raises(ImageNormalizationError) as error:
        service._dirs(7)  # type: ignore[attr-defined]

    assert error.value.code == "ATTACHMENT_STORAGE_UNAVAILABLE"


def test_dirs_creates_cosir_attachment_and_staging(tmp_path: Path) -> None:
    """``_dirs`` 首次调用须按需创建 ``.cosir`` / ``Attachment`` / ``.uploading``。"""

    root = tmp_path / "ws"
    root.mkdir()
    service = _service(root)

    service._dirs(7)  # type: ignore[attr-defined]

    assert (root / ".cosir").is_dir()
    assert (root / ".cosir" / "Attachment").is_dir()
    assert (root / ".cosir" / "Attachment" / ".uploading").is_dir()


# --- H. get_descriptor / resolve_content 回归（同属 7 个调用点） -------------------------


async def test_get_descriptor_and_resolve_content_after_finalize(tmp_path: Path) -> None:
    """finalize 后 get_descriptor / resolve_content 必须能在 ``.cosir/Attachment`` 定位文件。"""

    service = _service(tmp_path)
    data = _png_bytes()
    asset_id = hashlib.sha256(data).hexdigest()
    await service.upload(7, _upload(data))
    service.finalize(7, asset_id, _VISION_MODEL)

    descriptor = service.get_descriptor(7, asset_id)
    assert descriptor.asset_id == asset_id
    assert descriptor.locator == f"cosir-attachment://{asset_id}"
    assert descriptor.status == "ready"

    path, content_type = service.resolve_content(7, asset_id)
    assert path.parent == (tmp_path / ".cosir" / "Attachment").resolve()
    assert path.read_bytes() == data
    assert content_type == "image/png"


def test_get_descriptor_unknown_asset_not_found(tmp_path: Path) -> None:
    """未知 asset_id 的 get_descriptor 必须抛 ATTACHMENT_NOT_FOUND。"""

    service = _service(tmp_path)
    service._dirs(7)  # 确保目录存在  # type: ignore[attr-defined]

    with pytest.raises(ImageNormalizationError) as error:
        service.get_descriptor(7, "b" * 64)

    assert error.value.code == "ATTACHMENT_NOT_FOUND"


# --- I. image_utils 行为回归（is_image_path + is_trusted_cosir_path） -------------------


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("a.png", True),
        ("a.jpg", True),
        ("a.bmp", True),
        ("a.txt", False),
        ("", False),
        ("noext", False),
    ],
)
def test_is_image_path_contract(ref: str, expected: bool) -> None:
    """``is_image_path`` 扩展名判定契约（回归：image_utils 改动未破坏该函数）。"""

    from app.utils.image_utils import is_image_path

    assert is_image_path(ref) is expected


def test_is_trusted_cosir_path_does_not_raise_on_nul(tmp_path: Path) -> None:
    """含 NUL 的图片路径不得让 ``is_trusted_cosir_path`` 抛出（OSError 归一化）。"""

    from app.utils.image_utils import is_trusted_cosir_path

    root = tmp_path / "ws"
    (root / ".cosir").mkdir(parents=True)
    assert is_trusted_cosir_path("\x00bad", str(root)) is False
