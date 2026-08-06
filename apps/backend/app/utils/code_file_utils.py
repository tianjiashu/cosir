"""源代码文件扩展名判定工具。

单一职责：判断给定路径是否指向"源代码/可索引代码文件"。
不负责：路径解析、文件读写、索引构建、业务逻辑。

设计要点：
- 以扩展名为主要判定依据（不做文件内容嗅探，避免大文件 IO 开销）。
- 同时覆盖"语言源码"与"配置文件/标记语言"等常见可索引代码资产。
- 对无扩展名、隐藏文件、大小写、复合扩展名、目录等边界情况做显式处理。
- 提供灵活的白/黑名单扩展点，便于调用方按场景裁剪（如仅索引某种语言）。

判定分层（按顺序，命中即返回）：
1. 目录 / 复合排除（如 ``a.min.js``） / 文件名排除（如 ``package-lock.json``）→ False。
2. 点号隐藏配置文件名显式匹配（如 ``.env``/``.env.local``/``.gitignore``）→ True。
3. 扩展名在排除清单 → False；在白名单（含调用方追加）→ True；否则 False。

点号文件的特殊处理说明：
- ``pathlib`` 对 ``.env`` 的 ``suffix`` 为空串、对 ``.env.local`` 的 suffix 为 ``local``，
  纯靠"后缀白名单"无法稳定命中同类点号文件。因此对 ``.env``/`` .gitignore`` 这类
  **以文件名本身标识**的配置文件，采用显式文件名匹配（见 ``_HIDDEN_CONFIG_NAMES``），
  不把它们塞进后缀白名单（那是永不命中的死条目）。
- 无扩展名但**有文件名**的构建文件（如 ``Makefile``/``Dockerfile``）按当前口径
  返回 False（不索引）——若后续需要，应在 ``_HIDDEN_CONFIG_NAMES`` 之外另行显式声明。

与 CodeGraph 扩展名集合的关系（刻意解耦，避免漂移）：
- CodeGraph 的 ``third_party/codegraph/src/extraction/grammars.ts`` 中 ``EXTENSION_MAP``
  是"可被 tree-sitter 解析"的单一来源，口径偏窄（仅含有解析器的语言，不含 ``.md``/
  ``.json``/``.yaml`` 等纯索引项）。
- 本模块的口径偏宽：目标是"该文件是否值得进入语义索引（含配置/标记语言）"，
  二者用途不同，因此不复用同一集合，也不强行对齐；新增语言时两边需各自评估。
- 本模块已尽量对齐 CodeGraph 已支持的主流语言（如 ``.cs/.vue/.svelte/.astro/
  .sol/.mts/.cts/.ets``），但不保证完全同步。
- 二进制排除清单与 ``tools/tool_handler/read_file.py`` 的 ``binary_extensions`` 存在
  概念重叠，但后者用于"是否按二进制读取"，本模块用于"是否索引"；二者判定维度
  不同，不合并为同一集合，避免职责耦合。

调用方（当前迭代计划）：本工具将在 CodeGraph 索引 Hook 中使用——turn 前更新索引、
文件编辑工具成功后对代码文件触发 sync。详见 core/hook/builtins/codegraph_index_hook.py
（落地时接入）。
"""

from pathlib import Path

# 常见编程语言源代码扩展名（小写，不含点）。
# 口径：可被语义索引的代码/配置/标记文件，偏宽于 CodeGraph 的解析口径（见模块 docstring）。
_SOURCE_CODE_EXTENSIONS: frozenset[str] = frozenset(
    {
        # 通用脚本与系统语言
        "py",
        "pyi",
        "pyw",
        "js",
        "jsx",
        "ts",
        "tsx",
        "mjs",
        "cjs",
        "mts",
        "cts",  # TypeScript ESM/CJS 模块
        "c",
        "h",
        "cc",
        "cpp",
        "cxx",
        "hpp",
        "hh",
        "hxx",
        "rs",
        "go",
        "java",
        "kt",
        "kts",
        "scala",
        "sc",
        "swift",
        "m",
        "mm",
        "cs",
        "vb",  # C# / VB.NET
        "rb",
        "rake",
        "php",
        "module",
        "install",
        "theme",
        "inc",  # PHP 变体扩展名
        "pl",
        "pm",
        "lua",
        "luau",
        "r",
        "jl",
        "dart",
        "ex",
        "exs",
        "erl",
        "hrl",
        "elixir",
        "clj",
        "cljs",
        "fs",
        "fsi",
        "fsx",
        "hs",
        "ml",
        "mli",
        "nim",
        "zig",
        "v",
        "sql",
        "sol",  # Solidity
        "ets",  # ArkTS
        "vue",
        "svelte",
        "astro",
        "twig",
        "liquid",
        "metal",
        "cu",
        "cuh",  # Metal / CUDA（按 C++ 解析）
        "cob",
        "cobol",
        "cbl",
        "cpy",
        "pas",
        "dpr",
        "dpk",
        "lpr",
        "dfm",
        "fmx",
        "cfc",
        "cfm",
        "cfs",  # CFML / CFScript
        "nix",
        "tf",
        "tfvars",
        "tofu",  # Nix / Terraform
        "properties",
        "xml",
        "xsl",
        "xsd",
        # 数据与标记语言（可索引、可参与语义检索）
        "json",
        "jsonc",
        "yaml",
        "yml",
        "toml",
        "graphql",
        "gql",
        "proto",
        "html",
        "htm",
        "css",
        "scss",
        "sass",
        "less",
        "md",
        "mdx",
        "rst",
        "tex",
        "ipynb",
        # 构建与配置（常被索引以理解项目结构）
        "sh",
        "bash",
        "zsh",
        "ps1",
        "bat",
        "cmd",
        "make",
        "mk",
        "cmake",
        "ini",
        "cfg",
        "conf",
    }
)

# 明确排除的扩展名：易被误判为代码、但非源码资产的高噪文件。
_EXCLUDED_EXTENSIONS: frozenset[str] = frozenset(
    {
        "map",  # source map（a.js.map）
        "log",  # 日志
        "csv",
        "tsv",  # 数据而非代码
        "pdf",
        "doc",
        "docx",
        "xls",
        "xlsx",
        "ppt",
        "pptx",
        "png",
        "jpg",
        "jpeg",
        "gif",
        "svg",
        "ico",
        "webp",
        "bmp",
        "woff",
        "woff2",
        "ttf",
        "eot",
        "otf",
        "bin",
        "exe",
        "dll",
        "so",
        "dylib",
        "o",
        "a",
        "class",
        "jar",
        "zip",
        "gz",
        "tar",
        "7z",
        "rar",
        "pyc",
        "pyo",  # Python 字节码
    }
)

# 复合扩展名中间段：形如 ``a.min.js`` 的压缩产物，按最后一段（js）判定前需先剥离中间段。
_COMPOSITE_EXCLUDED_SUFFIXES: frozenset[str] = frozenset(
    {
        "min",  # *.min.js / *.min.css 压缩产物
    }
)

# 点号隐藏配置文件名（以文件名本身标识，非后缀）。这些文件 suffix 为空或不稳定，
# 无法靠后缀白名单命中，故在此显式按文件名匹配。仅收录"确实是配置、值得索引"的项。
_HIDDEN_CONFIG_NAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".env.local",
        ".env.development",
        ".env.production",
        ".env.test",
        ".gitignore",
        ".gitattributes",
        ".gitmodules",
        ".editorconfig",
        ".dockerignore",
        ".npmrc",
        ".npmignore",
        ".prettierrc",
        ".eslintrc",
        ".babelrc",
        ".nvmrc",
    }
)

# 文件名排除模式（前缀匹配，小写）：锁文件等高频高噪、不应索引的文件。
# 这类文件 suffix 往往是 json/yaml（会落入白名单），必须按文件名在 suffix 判定前拦截。
_EXCLUDED_FILENAME_PREFIXES: frozenset[str] = frozenset(
    {
        "package-lock.",  # package-lock.json
        "yarn.lock",  # yarn.lock
        "pnpm-lock.",  # pnpm-lock.yaml
        "poetry.lock",  # poetry.lock
        "uv.lock",  # uv.lock
        "gemfile.lock",  # Gemfile.lock
        "composer.lock",  # composer.lock
        "pipfile.lock",  # Pipfile.lock
    }
)


def get_source_code_extensions() -> frozenset[str]:
    """返回内置源代码扩展名集合（小写、不含点，不可变 frozenset）。

    返回:
        内置可索引代码扩展名的不可变集合（frozenset）。调用方无法修改其内容，
        也无法通过返回值误改内部状态；如需裁剪请使用 `is_source_code_file` 的
        `extra_extensions` / `exclude_extensions` 参数。
    """
    return _SOURCE_CODE_EXTENSIONS


def _normalize_suffix(path: Path) -> str:
    """从路径提取标准化的小写扩展名（不含点）。

    仅负责"取最后一个点之后的部分并小写"，不判断目录/点号文件等业务语义——
    这些由 `is_source_code_file` 负责。

    注意：隐藏文件（如 ``.env``）的 ``suffix`` 为空串、``.env.local`` 的 suffix 为
    ``local``；本函数不区分这些情形，统一返回最后一段或空串。

    参数:
        path: 待判定的路径（Path 对象）。

    返回:
        小写扩展名字符串（不含点）；无扩展名时返回空字符串。
        例：``a.min.js`` → ``"js"``、``foo.tar.gz`` → ``"gz"``、``.env`` → ``""``、
        ``.env.local`` → ``"local"``、``Dockerfile`` → ``""``。
    """
    suffix = path.suffix
    if not suffix:
        return ""
    return suffix.lower().lstrip(".")


def _has_composite_exclusion(path: Path) -> bool:
    """判断路径是否命中复合扩展名排除规则（如压缩产物 ``a.min.js``）。

    仅当路径含多个点、且中间段（倒数第二段）落在排除清单时命中。
    例如 ``a.min.js`` 的中间段为 ``min`` → 命中；``app.js`` 仅一段 → 不命中。

    参数:
        path: 待判定的路径（Path 对象）。

    返回:
        命中复合排除规则返回 True，否则 False。
    """
    parts = path.name.split(".")
    if len(parts) < 3:
        return False
    return parts[-2].lower() in _COMPOSITE_EXCLUDED_SUFFIXES


def _match_hidden_config(path: Path) -> bool | None:
    """按文件名显式匹配点号隐藏配置文件。

    用于覆盖"以文件名本身标识、suffix 为空或不稳定"的配置文件（见模块 docstring）。
    命中返回 True；明确排除的噪声点号文件（如 ``.DS_Store``）如需可在此扩展。
    不匹配任何已知点号配置时返回 None，交由扩展名逻辑继续判定。

    参数:
        path: 待判定的路径（Path 对象）。

    返回:
        命中已知点号配置返回 True；否则返回 None（交由调用方继续判定）。
    """
    name = path.name.lower()
    if name in _HIDDEN_CONFIG_NAMES:
        return True
    return None


def is_source_code_file(
    path: str | Path,
    *,
    extra_extensions: set[str] | None = None,
    exclude_extensions: set[str] | None = None,
) -> bool:
    """判断路径是否指向源代码/可索引代码文件。

    判定顺序（命中即返回）：
    1. 目录 → False。
    2. 复合扩展名排除（如 ``a.min.js``）→ False。
    3. 文件名排除模式（如 ``package-lock.json``）→ False。
    4. 点号隐藏配置文件名显式匹配（如 ``.env``/``.gitignore``）→ True。
    5. 无扩展名（且非上述点号配置）→ False（如 ``Makefile``/``README``）。
    6. 扩展名在排除清单 → False；在白名单（含调用方追加）→ True；否则 False。

    边界处理：
    - 目录 → False（不索引目录本身）。
    - 无扩展名文件（如 ``Makefile``、``README``）→ False。
    - 点号配置文件（``.env``/``.env.local``/``.gitignore``/``.editorconfig`` 等）→ True。
    - 大小写不敏感（``.PY`` 与 ``.py`` 等价；``.ENV`` 也命中）。
    - 复合扩展名只取最后一段（``a.min.js`` → 先被复合排除拦截 → False；
      ``foo.tar.gz`` → ``gz`` 在排除清单 → False）。
    - 仅做字符串判定，不读取文件内容，无 IO 开销。

    参数:
        path: 文件路径（字符串或 Path 对象）。
        extra_extensions: 在白名单基础上追加的扩展名集合（小写、不含点）；
            用于按项目场景裁剪，例如 {"vue", "svelte"}。
        exclude_extensions: 在排除清单基础上追加的扩展名集合（小写、不含点）；
            用于按项目场景剔除特定类型。

    返回:
        是源代码/可索引代码文件返回 True，否则 False。
    """
    p = Path(path)
    if p.is_dir():
        return False
    if _has_composite_exclusion(p):
        return False

    name_lower = p.name.lower()
    if any(name_lower.startswith(prefix) for prefix in _EXCLUDED_FILENAME_PREFIXES):
        return False

    hidden_match = _match_hidden_config(p)
    if hidden_match is not None:
        return hidden_match

    suffix = _normalize_suffix(p)
    if not suffix:
        return False

    if suffix in _EXCLUDED_EXTENSIONS:
        return False
    if exclude_extensions and suffix in exclude_extensions:
        return False

    if suffix in _SOURCE_CODE_EXTENSIONS:
        return True
    return bool(extra_extensions and suffix in extra_extensions)


def extract_source_code_paths(
    paths: list[str | Path],
    *,
    extra_extensions: set[str] | None = None,
    exclude_extensions: set[str] | None = None,
) -> list[Path]:
    """从路径列表中筛选源代码文件，过滤目录、无扩展名与排除项。

    参数:
        paths: 待筛选的路径列表（字符串或 Path 混合）。
        extra_extensions: 追加白名单扩展名；见 `is_source_code_file`。
        exclude_extensions: 追加排除扩展名；见 `is_source_code_file`。

    返回:
        按输入顺序保留、判定为代码文件的 Path 列表（去重保序）。

    副作用:
        无。
    """
    seen: set[Path] = set()
    result: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p in seen:
            continue
        if is_source_code_file(
            p,
            extra_extensions=extra_extensions,
            exclude_extensions=exclude_extensions,
        ):
            seen.add(p)
            result.append(p)
    return result
