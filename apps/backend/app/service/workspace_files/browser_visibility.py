"""文件浏览器的精确路径设置；独立于 Agent 的 .fileignore 和访问权限。

规则按展示路径匹配，不解析链接目标；隐藏链接不会隐藏其实际目标。
配置缺失视为空且不落盘，恢复已删除路径也不要求磁盘条目仍存在。
"""

import hashlib
import json
import os
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.config.logging.logger import log
from app.service.configuration.file_store import ConfigurationFileStore


def normalize_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\x00" in value
        or path.is_absolute()
        or ".." in path.parts
        or path == PurePosixPath(".")
        or (os.name == "nt" and ("\\" in value or ":" in value))
    ):
        raise ValueError("忽略项必须是工作区内的非根相对路径")
    return path.as_posix()


class _Document(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ignored_paths: list[str] = Field(default_factory=list)

    @field_validator("ignored_paths")
    @classmethod
    def normalize(cls, values: list[str]) -> list[str]:
        return sorted({normalize_path(value) for value in values})


@dataclass(frozen=True)
class BrowserVisibility:
    ignored_paths: tuple[str, ...]
    revision: str

    @cached_property
    def _paths(self) -> frozenset[str]:
        return frozenset(self.ignored_paths)

    def hides(self, relative: str) -> bool:
        # 逐段判断，避免 src/a 误伤 src/abc；文件改成目录后仍沿用同一路径设置。
        path = PurePosixPath(relative)
        return any(str(part) in self._paths for part in (path, *path.parents))


@dataclass(frozen=True)
class VisibilityChange:
    settings: BrowserVisibility
    changed_paths: tuple[str, ...]


class BrowserVisibilityStore:
    """只由浏览用例调用；锁覆盖读改写，撤销仅反向修改本次实际变更的路径。"""

    def __init__(self, root: Path) -> None:
        self.root = root / ".cosir"
        self.path = self.root / "file-browser.json"
        self.store = ConfigurationFileStore()

    def read(self) -> BrowserVisibility:
        if not self.path.exists():
            return self._settings(_Document())
        content = self.store.read_text(self.path, root=self.root)
        try:
            document = _Document.model_validate_json(content)
        except ValidationError as exc:
            log.warning(
                "workspace_browser_settings_invalid",
                extra={
                    "msg": "文件浏览器忽略配置无效",
                    "data": {"path": str(self.path)},
                },
            )
            raise ValueError("文件浏览器忽略配置无效，请检查 .cosir/file-browser.json") from exc
        return self._settings(document)

    def change(self, paths: list[str], *, ignored: bool) -> VisibilityChange:
        requested = set(_Document(ignored_paths=paths).ignored_paths)
        with self.store.locked(self.path):
            current = set(self.read().ignored_paths)
            changed = requested - current if ignored else requested & current
            updated = current | requested if ignored else current - requested
            document = _Document.model_construct(ignored_paths=sorted(updated))
            if changed:
                self.store.write_text_atomic(
                    self.path,
                    document.model_dump_json(indent=2) + "\n",
                    root=self.root,
                )
                log.info(
                    "workspace_browser_visibility_changed",
                    extra={
                        "msg": "文件浏览器忽略设置已更新",
                        "data": {
                            "workspace_root": str(self.root.parent),
                            "ignored": ignored,
                            "paths": sorted(changed),
                        },
                    },
                )
            return VisibilityChange(self._settings(document), tuple(sorted(changed)))

    @staticmethod
    def _settings(document: _Document) -> BrowserVisibility:
        paths = tuple(document.ignored_paths)
        revision = hashlib.sha256(json.dumps(paths, ensure_ascii=False).encode()).hexdigest()
        return BrowserVisibility(paths, revision)
