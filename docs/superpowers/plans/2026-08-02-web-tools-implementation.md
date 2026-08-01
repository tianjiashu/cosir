# Web Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add first-class `web_search` and `web_extract` tools to the backend tool system, modeled after Hermes' complete Web tool behavior but integrated into this project's `ToolDefinition`/`ToolSystem` architecture.

**Architecture:** Web tools live inside `apps/backend/app/tools/tool_handler/web/` and expose only two model-visible tools: `web_search` for result metadata and `web_extract` for page content. Provider selection is a thin internal registry with search/extract capability routing, URL safety runs before any outbound extract request, and oversized extracted content is stored under workspace-local `.coding-agent/tool-results/web/` files with `read_file` paging guidance.

**Tech Stack:** Python 3.11, Pydantic v2, existing `httpx==0.28.1`, FastAPI backend tool system, pytest, Ruff, mypy, SQLite-free file storage for extracted page snapshots.

## Global Constraints

- Do not implement `python_execute`; this plan only covers `web_search` and `web_extract`.
- Preserve current project architecture: `ToolDefinition` remains the single source of truth, and `ToolSystem.build_tool_system()` explicitly registers built-in tools.
- Do not add a Hermes-style module-level global tool registry.
- Search returns metadata only: title, URL, description, position, provider.
- Extract returns cleaned page content with no LLM summarization.
- `web_extract` must accept URL strings and search-result objects containing string `url` or `href`.
- `web_extract` must reject embedded secrets, credential-like query parameters, non-HTTP(S) schemes, localhost/private/internal IPs, and more than 5 URLs per call before provider I/O.
- Provider `is_available()` must be cheap and must not perform network calls.
- Explicit backend config wins for diagnostics: if configured backend exists and supports the requested capability, return it even when unavailable so the caller can report the missing credential precisely.
- If configured backend exists but does not support extract, `web_extract` returns a deterministic search-only backend error.
- Unit tests must not perform real network calls.
- Tool output passed to the model must be concise English; Python docstrings remain Chinese per project convention.
- New Python functions and methods must have complete Chinese docstrings with 参数 / 返回 / 异常 / 副作用 sections.
- Keep compatibility with macOS and Windows path behavior by using `pathlib.Path`.
- Do not store fetched Web content outside the current workspace root.

---

## File Structure

- Create `apps/backend/app/tools/tool_handler/web/__init__.py`: package marker and public re-exports only.
- Create `apps/backend/app/tools/tool_handler/web/web_provider.py`: provider protocol, normalized result dataclasses, provider exceptions, and config-aware env lookup.
- Create `apps/backend/app/tools/tool_handler/web/web_provider_registry.py`: provider registration and active provider resolution.
- Create `apps/backend/app/tools/tool_handler/web/url_safety.py`: URL normalization, secret detection, sensitive query detection, and SSRF/private-network blocking.
- Create `apps/backend/app/tools/tool_handler/web/web_content_store.py`: base64 image placeholder conversion, head/tail truncation, workspace-local full-content storage.
- Create `apps/backend/app/tools/tool_handler/web/web_search.py`: `WebSearchTool` handler and `build_web_search_definition()`.
- Create `apps/backend/app/tools/tool_handler/web/web_extract.py`: `WebExtractTool` handler and `build_web_extract_definition()`.
- Create `apps/backend/app/tools/tool_handler/web/providers/__init__.py`: provider registration helper.
- Create provider files under `apps/backend/app/tools/tool_handler/web/providers/`: `tavily_provider.py`, `firecrawl_provider.py`, `exa_provider.py`, `parallel_provider.py`, `searxng_provider.py`, `brave_provider.py`, `ddgs_provider.py`.
- Create `apps/backend/app/tools/tool_models/web_search_args.py`: strict Pydantic args model for `web_search`.
- Create `apps/backend/app/tools/tool_models/web_extract_args.py`: strict Pydantic args model for `web_extract`.
- Modify `apps/backend/app/tools/tool_system.py`: register both Web tool definitions after existing built-ins.
- Modify `apps/backend/app/config/settings.py`: add Web backend, timeout, and output settings.
- Modify `apps/backend/pyproject.toml` and `apps/backend/uv.lock` only if `ddgs` is added as an optional direct dependency; all HTTP providers use existing `httpx`.
- Add tests in `apps/backend/tests/`: `test_web_provider_registry.py`, `test_web_url_safety.py`, `test_web_content_store.py`, `test_web_search_tool.py`, `test_web_extract_tool.py`, `test_tool_system_web_tools.py`, and provider normalizer tests.

## Provider Interface

```python
@dataclass(frozen=True)
class WebSearchItem:
    title: str
    url: str
    description: str
    position: int


@dataclass(frozen=True)
class WebExtractItem:
    url: str
    title: str
    content: str
    raw_content: str
    metadata: dict[str, object]
    error: str = ""


class WebProvider(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def display_name(self) -> str: ...
    def is_available(self) -> bool: ...
    def supports_search(self) -> bool: ...
    def supports_extract(self) -> bool: ...
    def missing_configuration_message(self) -> str: ...
    def search(self, query: str, limit: int) -> list[WebSearchItem]: ...
    def extract(self, urls: list[str], output_format: str) -> list[WebExtractItem] | Awaitable[list[WebExtractItem]]: ...
```

## Task 1: Settings And Provider Registry

**Files:**
- Create: `apps/backend/app/tools/tool_handler/web/web_provider.py`
- Create: `apps/backend/app/tools/tool_handler/web/web_provider_registry.py`
- Create: `apps/backend/app/tools/tool_handler/web/__init__.py`
- Modify: `apps/backend/app/config/settings.py`
- Test: `apps/backend/tests/test_web_provider_registry.py`

**Interfaces:**
- Consumes: `Settings.override(**kwargs)` and class-level `Settings` configuration.
- Produces: `WebSearchItem`, `WebExtractItem`, `WebProvider`, `WebProviderUnavailableError`, `WebProviderRegistry`, `register_default_web_providers()`, `get_active_search_provider()`, `get_active_extract_provider()`.

- [ ] **Step 1: Write the failing registry tests**

```python
from dataclasses import dataclass

import pytest

from app.config.settings import Settings
from app.tools.tool_handler.web.web_provider import WebSearchItem
from app.tools.tool_handler.web.web_provider_registry import WebProviderRegistry


@dataclass
class FakeProvider:
    name: str
    available: bool = True
    search: bool = True
    extract: bool = True

    @property
    def display_name(self) -> str:
        return self.name

    def is_available(self) -> bool:
        return self.available

    def supports_search(self) -> bool:
        return self.search

    def supports_extract(self) -> bool:
        return self.extract

    def missing_configuration_message(self) -> str:
        return f"{self.name} is not configured"

    def search(self, query: str, limit: int) -> list[WebSearchItem]:
        return []


def test_explicit_search_backend_wins_even_when_unavailable():
    registry = WebProviderRegistry()
    registry.register(FakeProvider("tavily", available=False))
    registry.register(FakeProvider("exa", available=True))

    provider = registry.active_search_provider(explicit_backend="tavily")

    assert provider is not None
    assert provider.name == "tavily"


def test_extract_rejects_search_only_explicit_backend_without_fallback():
    registry = WebProviderRegistry()
    registry.register(FakeProvider("brave-free", available=True, extract=False))
    registry.register(FakeProvider("firecrawl", available=True, extract=True))

    provider = registry.active_extract_provider(explicit_backend="brave-free")

    assert provider is not None
    assert provider.name == "brave-free"
    assert provider.supports_extract() is False


def test_fallback_uses_legacy_priority_filtered_by_capability_and_availability():
    registry = WebProviderRegistry()
    registry.register(FakeProvider("exa", available=True, extract=True))
    registry.register(FakeProvider("firecrawl", available=False, extract=True))
    registry.register(FakeProvider("brave-free", available=True, extract=False))

    assert registry.active_search_provider(explicit_backend="").name == "exa"
    assert registry.active_extract_provider(explicit_backend="").name == "exa"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/test_web_provider_registry.py -v`

Expected: FAIL because `app.tools.tool_handler.web.web_provider` and registry modules do not exist.

- [ ] **Step 3: Implement provider primitives and registry**

Implement `WebProviderRegistry` with these exact methods:

```python
class WebProviderRegistry:
    def register(self, provider: WebProvider) -> None: ...
    def list_providers(self) -> list[WebProvider]: ...
    def get_provider(self, name: str) -> WebProvider | None: ...
    def active_search_provider(self, explicit_backend: str = "") -> WebProvider | None: ...
    def active_extract_provider(self, explicit_backend: str = "") -> WebProvider | None: ...
```

Selection order:

```python
LEGACY_PROVIDER_PRIORITY = (
    "firecrawl",
    "parallel",
    "tavily",
    "exa",
    "searxng",
    "brave-free",
    "ddgs",
)
```

Add settings:

```python
WEB_SEARCH_BACKEND: ClassVar[str] = ""
WEB_EXTRACT_BACKEND: ClassVar[str] = ""
WEB_BACKEND: ClassVar[str] = ""
WEB_REQUEST_TIMEOUT_SECONDS: ClassVar[float] = 20.0
WEB_SEARCH_LIMIT_MAX: ClassVar[int] = 20
WEB_EXTRACT_URL_LIMIT_MAX: ClassVar[int] = 5
WEB_EXTRACT_CHAR_LIMIT: ClassVar[int] = 15000
WEB_EXTRACT_STORE_DIR_NAME: ClassVar[str] = ".coding-agent/tool-results/web"
```

Load env values:

```python
cls.WEB_SEARCH_BACKEND = os.environ.get("CODING_AGENT_WEB_SEARCH_BACKEND", "").strip().lower()
cls.WEB_EXTRACT_BACKEND = os.environ.get("CODING_AGENT_WEB_EXTRACT_BACKEND", "").strip().lower()
cls.WEB_BACKEND = os.environ.get("CODING_AGENT_WEB_BACKEND", "").strip().lower()
cls.WEB_REQUEST_TIMEOUT_SECONDS = float(os.environ.get("CODING_AGENT_WEB_REQUEST_TIMEOUT_SECONDS", "20"))
cls.WEB_SEARCH_LIMIT_MAX = int(os.environ.get("CODING_AGENT_WEB_SEARCH_LIMIT_MAX", "20"))
cls.WEB_EXTRACT_URL_LIMIT_MAX = int(os.environ.get("CODING_AGENT_WEB_EXTRACT_URL_LIMIT_MAX", "5"))
cls.WEB_EXTRACT_CHAR_LIMIT = int(os.environ.get("CODING_AGENT_WEB_EXTRACT_CHAR_LIMIT", "15000"))
cls.WEB_EXTRACT_STORE_DIR_NAME = os.environ.get(
    "CODING_AGENT_WEB_EXTRACT_STORE_DIR_NAME",
    ".coding-agent/tool-results/web",
)
```

Add validation for positive numeric values.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/test_web_provider_registry.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/app/config/settings.py apps/backend/app/tools/tool_handler/web apps/backend/tests/test_web_provider_registry.py
git commit -m "feat: add web provider registry"
```

## Task 2: URL Safety Gate

**Files:**
- Create: `apps/backend/app/tools/tool_handler/web/url_safety.py`
- Test: `apps/backend/tests/test_web_url_safety.py`

**Interfaces:**
- Consumes: standard library `urllib.parse`, `ipaddress`, `socket`.
- Produces: `extract_url_from_item(value: object) -> str | None`, `normalize_url_for_request(url: str) -> str`, `sensitive_query_param_name(url: str) -> str | None`, `url_contains_secret(url: str) -> bool`, `is_safe_public_url(url: str, resolver: Callable[[str], Iterable[str]] | None = None) -> tuple[bool, str]`.

- [ ] **Step 1: Write the failing URL safety tests**

```python
from app.tools.tool_handler.web.url_safety import (
    extract_url_from_item,
    is_safe_public_url,
    normalize_url_for_request,
    sensitive_query_param_name,
    url_contains_secret,
)


def test_extract_url_accepts_string_and_search_result_objects():
    assert extract_url_from_item(" https://example.com/a ") == "https://example.com/a"
    assert extract_url_from_item({"url": "https://example.com/u"}) == "https://example.com/u"
    assert extract_url_from_item({"href": "https://example.com/h"}) == "https://example.com/h"
    assert extract_url_from_item({"href": 3}) is None


def test_normalize_adds_https_when_scheme_missing():
    assert normalize_url_for_request("example.com/docs") == "https://example.com/docs"


def test_blocks_secret_like_url_values():
    assert url_contains_secret("https://example.com/?token=sk-test") is True
    assert url_contains_secret("https://example.com/?q=%73%6b-test") is True


def test_detects_credential_query_param_names():
    assert sensitive_query_param_name("https://example.com/?api_key=abc") == "api_key"
    assert sensitive_query_param_name("https://example.com/?page=abc") is None


def test_rejects_private_ip_from_resolver():
    safe, reason = is_safe_public_url(
        "https://metadata.google.internal/latest",
        resolver=lambda host: ["169.254.169.254"],
    )

    assert safe is False
    assert "private or internal" in reason


def test_rejects_non_http_scheme():
    safe, reason = is_safe_public_url("file:///etc/passwd", resolver=lambda host: [])

    assert safe is False
    assert "http or https" in reason
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/test_web_url_safety.py -v`

Expected: FAIL because `url_safety.py` does not exist.

- [ ] **Step 3: Implement URL safety functions**

Use a module-level secret regex equivalent in spirit to Hermes' redaction prefix check:

```python
SECRET_VALUE_RE = re.compile(
    r"(?i)(sk-[a-z0-9_-]{8,}|xox[baprs]-[a-z0-9-]{8,}|gh[pousr]_[a-z0-9_]{12,}|"
    r"api[_-]?key[=:][^&\\s]+|bearer\\s+[a-z0-9._-]{12,})"
)
SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "client_secret",
    "code",
    "id_token",
    "jwt",
    "key",
    "password",
    "refresh_token",
    "secret",
    "session",
    "sig",
    "signature",
    "token",
}
```

`is_safe_public_url()` must:

- Normalize first.
- Permit only `http` and `https`.
- Require a hostname.
- Resolve all host addresses through the supplied resolver or `socket.getaddrinfo`.
- Reject loopback, private, link-local, multicast, reserved, unspecified, and site-local IP addresses via `ipaddress.ip_address`.
- Return `(True, "")` for safe URLs and `(False, "Blocked: ...")` for blocked URLs.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/test_web_url_safety.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/app/tools/tool_handler/web/url_safety.py apps/backend/tests/test_web_url_safety.py
git commit -m "feat: add web url safety checks"
```

## Task 3: Web Content Store And Truncation

**Files:**
- Create: `apps/backend/app/tools/tool_handler/web/web_content_store.py`
- Test: `apps/backend/tests/test_web_content_store.py`

**Interfaces:**
- Consumes: `ToolExecutionContext`, `Settings.WEB_EXTRACT_STORE_DIR_NAME`.
- Produces: `StoredWebContent`, `convert_base64_images_to_placeholders(markdown: str) -> str`, `truncate_or_store_content(content: str, url: str, title: str, execution_context: ToolExecutionContext, char_limit: int) -> tuple[str, StoredWebContent | None]`.

- [ ] **Step 1: Write the failing content-store tests**

```python
from pathlib import Path

from app.tools.schemas.tool_execution_context import ToolExecutionContext
from app.tools.tool_handler.web.web_content_store import (
    convert_base64_images_to_placeholders,
    truncate_or_store_content,
)


def test_replaces_inline_base64_images_with_placeholder():
    content = "Before ![diagram](data:image/png;base64,AAAABBBB) after"

    assert convert_base64_images_to_placeholders(content) == "Before [IMAGE: diagram] after"


def test_stores_full_content_and_returns_read_file_footer(tmp_path: Path):
    context = ToolExecutionContext(
        task_id="task_1",
        workspace_id="workspace_1",
        workspace_root=tmp_path,
    )
    content = "A" * 80 + "MIDDLE" + "Z" * 80

    truncated, stored = truncate_or_store_content(
        content=content,
        url="https://example.com/article",
        title="Article",
        execution_context=context,
        char_limit=80,
    )

    assert stored is not None
    assert stored.path.is_file()
    assert stored.relative_path.startswith(".coding-agent/tool-results/web/")
    assert "Content truncated" in truncated
    assert "read_file" in truncated
    assert "MIDDLE" not in truncated
    assert stored.path.read_text(encoding="utf-8") == content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/test_web_content_store.py -v`

Expected: FAIL because `web_content_store.py` does not exist.

- [ ] **Step 3: Implement content store**

Implement stable, workspace-local filenames:

```python
digest = hashlib.sha256(f"{url}\n{content}".encode("utf-8")).hexdigest()[:16]
safe_title = re.sub(r"[^A-Za-z0-9._-]+", "-", title.strip() or "page").strip("-")[:48]
relative_path = Path(Settings.WEB_EXTRACT_STORE_DIR_NAME) / f"{digest}-{safe_title}.md"
```

`truncate_or_store_content()` must:

- Return original content and `None` when `len(content) <= char_limit`.
- Store the full content using `Path.write_text(encoding="utf-8")` after creating parent directories under `execution_context.workspace_root`.
- Return head/tail content split as `head_budget = char_limit // 2` and `tail_budget = char_limit - head_budget`.
- Append footer:

```text

[Content truncated. Full content saved to <relative_path>. Use read_file with that path and offset/limit paging to inspect omitted sections.]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/test_web_content_store.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/app/tools/tool_handler/web/web_content_store.py apps/backend/tests/test_web_content_store.py
git commit -m "feat: store oversized web extraction content"
```

## Task 4: Provider Implementations

**Files:**
- Create: `apps/backend/app/tools/tool_handler/web/providers/__init__.py`
- Create: `apps/backend/app/tools/tool_handler/web/providers/tavily_provider.py`
- Create: `apps/backend/app/tools/tool_handler/web/providers/firecrawl_provider.py`
- Create: `apps/backend/app/tools/tool_handler/web/providers/exa_provider.py`
- Create: `apps/backend/app/tools/tool_handler/web/providers/parallel_provider.py`
- Create: `apps/backend/app/tools/tool_handler/web/providers/searxng_provider.py`
- Create: `apps/backend/app/tools/tool_handler/web/providers/brave_provider.py`
- Create: `apps/backend/app/tools/tool_handler/web/providers/ddgs_provider.py`
- Test: `apps/backend/tests/test_web_providers.py`

**Interfaces:**
- Consumes: `WebProvider`, `WebSearchItem`, `WebExtractItem`, `Settings.WEB_REQUEST_TIMEOUT_SECONDS`.
- Produces: `default_web_providers() -> list[WebProvider]`.

- [ ] **Step 1: Write failing provider normalizer tests**

```python
from app.tools.tool_handler.web.providers.brave_provider import BraveProvider
from app.tools.tool_handler.web.providers.searxng_provider import SearxngProvider


def test_brave_normalizes_search_results(monkeypatch):
    provider = BraveProvider(api_key="key")

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "web": {
                    "results": [
                        {
                            "title": "Title",
                            "url": "https://example.com",
                            "description": "Desc",
                        }
                    ]
                }
            }

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, params, headers):
            return FakeResponse()

    monkeypatch.setattr("app.tools.tool_handler.web.providers.brave_provider.httpx.Client", lambda timeout: FakeClient())

    results = provider.search("query", 3)

    assert results[0].title == "Title"
    assert results[0].url == "https://example.com"
    assert results[0].description == "Desc"
    assert results[0].position == 1


def test_searxng_normalizes_search_results(monkeypatch):
    provider = SearxngProvider(base_url="https://search.example")

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "results": [
                    {"title": "A", "url": "https://a.example", "content": "Alpha"},
                    {"title": "B", "url": "https://b.example", "content": "Beta"},
                ]
            }

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, params):
            return FakeResponse()

    monkeypatch.setattr("app.tools.tool_handler.web.providers.searxng_provider.httpx.Client", lambda timeout: FakeClient())

    results = provider.search("query", 2)

    assert [item.position for item in results] == [1, 2]
    assert [item.description for item in results] == ["Alpha", "Beta"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/test_web_providers.py -v`

Expected: FAIL because provider modules do not exist.

- [ ] **Step 3: Implement provider files**

Provider behavior:

- `TavilyProvider`: search and extract, available when `TAVILY_API_KEY` exists.
- `FirecrawlProvider`: search and extract, available when `FIRECRAWL_API_KEY` or `FIRECRAWL_API_URL` exists.
- `ExaProvider`: search and extract, available when `EXA_API_KEY` exists.
- `ParallelProvider`: search and extract, available when `PARALLEL_API_KEY` exists.
- `SearxngProvider`: search only, available when `SEARXNG_URL` exists.
- `BraveProvider`: search only, available when `BRAVE_SEARCH_API_KEY` exists.
- `DdgsProvider`: search only, available when the `ddgs` package imports.

Use `httpx.Client(timeout=Settings.WEB_REQUEST_TIMEOUT_SECONDS)` or `httpx.AsyncClient(timeout=...)`. Keep raw SDK dependencies out of the first implementation except `ddgs`; HTTP providers should construct requests directly.

Normalize every search result into `WebSearchItem(title, url, description, position)` and every extract result into `WebExtractItem(url, title, content, raw_content, metadata)`.

`default_web_providers()` returns provider instances in this order:

```python
return [
    FirecrawlProvider(),
    ParallelProvider(),
    TavilyProvider(),
    ExaProvider(),
    SearxngProvider(),
    BraveProvider(),
    DdgsProvider(),
]
```

- [ ] **Step 4: Run provider tests**

Run: `cd apps/backend && uv run pytest tests/test_web_providers.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/app/tools/tool_handler/web/providers apps/backend/tests/test_web_providers.py
git commit -m "feat: add web providers"
```

## Task 5: web_search Tool

**Files:**
- Create: `apps/backend/app/tools/tool_models/web_search_args.py`
- Create: `apps/backend/app/tools/tool_handler/web/web_search.py`
- Modify: `apps/backend/app/tools/tool_system.py`
- Test: `apps/backend/tests/test_web_search_tool.py`
- Test: `apps/backend/tests/test_tool_system_web_tools.py`

**Interfaces:**
- Consumes: `WebProviderRegistry`, `Settings.WEB_SEARCH_BACKEND`, `Settings.WEB_BACKEND`, `tool_success()`, `tool_error()`.
- Produces: `WebSearchTool.execute(query: str, limit: int = 5, execution_context: ToolExecutionContext | None = None) -> ToolObservation`, `build_web_search_definition() -> ToolDefinition`.

- [ ] **Step 1: Write failing tool tests**

```python
import json

from app.tools.tool_handler.web.web_provider import WebSearchItem
from app.tools.tool_handler.web.web_provider_registry import WebProviderRegistry
from app.tools.tool_handler.web.web_search import WebSearchTool


class FakeSearchProvider:
    name = "fake"
    display_name = "Fake"

    def is_available(self) -> bool:
        return True

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def missing_configuration_message(self) -> str:
        return "Fake is not configured"

    def search(self, query: str, limit: int) -> list[WebSearchItem]:
        return [WebSearchItem("Title", "https://example.com", "Desc", 1)]


def test_web_search_returns_metadata_only():
    registry = WebProviderRegistry()
    registry.register(FakeSearchProvider())
    tool = WebSearchTool(provider_registry=registry)

    observation = tool.execute("python", limit=1)
    payload = json.loads(observation.content)

    assert observation.status == "success"
    assert payload["success"] is True
    assert payload["data"]["web"][0] == {
        "title": "Title",
        "url": "https://example.com",
        "description": "Desc",
        "position": 1,
        "provider": "fake",
    }
    assert "content" not in payload["data"]["web"][0]


def test_web_search_reports_missing_provider():
    tool = WebSearchTool(provider_registry=WebProviderRegistry())

    observation = tool.execute("python", limit=1)

    assert observation.status == "error"
    assert "No web search provider configured" in observation.error
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/test_web_search_tool.py tests/test_tool_system_web_tools.py -v`

Expected: FAIL because `web_search.py` and args model do not exist.

- [ ] **Step 3: Implement `web_search`**

Args model:

```python
class WebSearchArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    query: str = Field(min_length=1, description="Search query.")
    limit: int = Field(default=5, ge=1, description="Maximum search results to return.")
```

Tool behavior:

- Clamp `limit` to `Settings.WEB_SEARCH_LIMIT_MAX`.
- Select backend from `Settings.WEB_SEARCH_BACKEND or Settings.WEB_BACKEND`.
- Return `tool_error()` when no provider exists, provider lacks search support, provider unavailable, or provider raises.
- Return `tool_success()` with JSON:

```json
{
  "success": true,
  "data": {
    "web": [
      {
        "title": "Title",
        "url": "https://example.com",
        "description": "Desc",
        "position": 1,
        "provider": "fake"
      }
    ]
  }
}
```

Definition metadata:

```python
name = "web_search"
permission = "network"
risk_level = "medium"
resource_keys = ("network",)
timeout_seconds = Settings.WEB_REQUEST_TIMEOUT_SECONDS + 5
execution_mode = "thread"
```

- [ ] **Step 4: Register `web_search` in ToolSystem and test**

Add:

```python
from app.tools.tool_handler.web.web_search import build_web_search_definition

registry.register(build_web_search_definition())
```

Tool system test:

```python
from app.tools.tool_system import ToolSystem


def test_tool_system_registers_web_tools():
    system = ToolSystem.build_tool_system()

    assert system.registry.get_tool_definition("web_search") is not None
```

Run: `cd apps/backend && uv run pytest tests/test_web_search_tool.py tests/test_tool_system_web_tools.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/app/tools/tool_models/web_search_args.py apps/backend/app/tools/tool_handler/web/web_search.py apps/backend/app/tools/tool_system.py apps/backend/tests/test_web_search_tool.py apps/backend/tests/test_tool_system_web_tools.py
git commit -m "feat: add web search tool"
```

## Task 6: web_extract Tool

**Files:**
- Create: `apps/backend/app/tools/tool_models/web_extract_args.py`
- Create: `apps/backend/app/tools/tool_handler/web/web_extract.py`
- Modify: `apps/backend/app/tools/tool_system.py`
- Test: `apps/backend/tests/test_web_extract_tool.py`
- Test: `apps/backend/tests/test_tool_system_web_tools.py`

**Interfaces:**
- Consumes: `url_safety`, `web_content_store`, `WebProviderRegistry`, `Settings.WEB_EXTRACT_BACKEND`, `Settings.WEB_BACKEND`, `Settings.WEB_EXTRACT_CHAR_LIMIT`, `Settings.WEB_EXTRACT_URL_LIMIT_MAX`.
- Produces: `WebExtractTool.execute(urls: list[object], format: Literal["markdown", "html", "text"] = "markdown", char_limit: int | None = None, execution_context: ToolExecutionContext | None = None) -> ToolObservation`, `build_web_extract_definition() -> ToolDefinition`.

- [ ] **Step 1: Write failing extract tests**

```python
import json
from pathlib import Path

from app.tools.schemas.tool_execution_context import ToolExecutionContext
from app.tools.tool_handler.web.web_extract import WebExtractTool
from app.tools.tool_handler.web.web_provider import WebExtractItem
from app.tools.tool_handler.web.web_provider_registry import WebProviderRegistry


class FakeExtractProvider:
    name = "fake"
    display_name = "Fake"

    def is_available(self) -> bool:
        return True

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    def missing_configuration_message(self) -> str:
        return "Fake is not configured"

    def extract(self, urls: list[str], output_format: str):
        return [
            WebExtractItem(
                url=urls[0],
                title="Page",
                content="Hello world",
                raw_content="Hello world",
                metadata={"source": "fake"},
            )
        ]


def test_web_extract_accepts_search_result_objects(tmp_path: Path):
    registry = WebProviderRegistry()
    registry.register(FakeExtractProvider())
    tool = WebExtractTool(provider_registry=registry, resolver=lambda host: ["93.184.216.34"])
    context = ToolExecutionContext("task_1", "workspace_1", tmp_path)

    observation = tool.execute([{"url": "example.com/page"}], execution_context=context)
    payload = json.loads(observation.content)

    assert observation.status == "success"
    assert payload["success"] is True
    assert payload["results"][0]["url"] == "https://example.com/page"
    assert payload["results"][0]["content"] == "Hello world"


def test_web_extract_blocks_secret_urls(tmp_path: Path):
    tool = WebExtractTool(provider_registry=WebProviderRegistry(), resolver=lambda host: ["93.184.216.34"])
    context = ToolExecutionContext("task_1", "workspace_1", tmp_path)

    observation = tool.execute(["https://example.com/?api_key=secret"], execution_context=context)

    assert observation.status == "error"
    assert "credential-like query parameter" in observation.error


def test_web_extract_requires_execution_context():
    registry = WebProviderRegistry()
    registry.register(FakeExtractProvider())
    tool = WebExtractTool(provider_registry=registry, resolver=lambda host: ["93.184.216.34"])

    observation = tool.execute(["https://example.com"])

    assert observation.status == "error"
    assert "execution context" in observation.error
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/test_web_extract_tool.py tests/test_tool_system_web_tools.py -v`

Expected: FAIL because `web_extract.py` and args model do not exist.

- [ ] **Step 3: Implement `web_extract`**

Args model:

```python
class WebExtractArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    urls: list[object] = Field(min_length=1, description="URL strings or search result objects with url/href.")
    format: Literal["markdown", "html", "text"] = Field(default="markdown", description="Desired extraction format.")
    char_limit: int | None = Field(default=None, ge=1, description="Maximum characters per page returned to the model.")
```

Tool behavior:

- Require `execution_context` because full extracted content may be stored under the workspace.
- Reject more than `Settings.WEB_EXTRACT_URL_LIMIT_MAX` input URLs.
- Normalize each item through `extract_url_from_item()`.
- Run secret, sensitive-query, scheme, hostname, and private-network checks before provider I/O.
- Select backend from `Settings.WEB_EXTRACT_BACKEND or Settings.WEB_BACKEND`.
- If selected provider exists but `supports_extract()` is false, return:

```text
<display_name> is a search-only backend and cannot extract URL content. Configure an extract-capable backend such as firecrawl, tavily, exa, or parallel.
```

- If provider.extract is async, run it with a private event loop from the synchronous handler; if provider.extract is sync, run directly. Keep the tool definition `execution_mode="thread"`.
- For each successful page, call `convert_base64_images_to_placeholders()` then `truncate_or_store_content()`.
- Return JSON:

```json
{
  "success": true,
  "results": [
    {
      "url": "https://example.com",
      "title": "Page",
      "content": "Hello world",
      "provider": "fake",
      "stored_path": "",
      "truncated": false,
      "metadata": {"source": "fake"}
    }
  ]
}
```

Definition metadata:

```python
name = "web_extract"
permission = "network"
risk_level = "medium"
resource_keys = ("network", "filesystem")
timeout_seconds = Settings.WEB_REQUEST_TIMEOUT_SECONDS + 10
execution_mode = "thread"
```

- [ ] **Step 4: Register `web_extract` in ToolSystem and test**

Add:

```python
from app.tools.tool_handler.web.web_extract import build_web_extract_definition

registry.register(build_web_extract_definition())
```

Extend tool system test:

```python
assert system.registry.get_tool_definition("web_extract") is not None
```

Run: `cd apps/backend && uv run pytest tests/test_web_extract_tool.py tests/test_tool_system_web_tools.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/app/tools/tool_models/web_extract_args.py apps/backend/app/tools/tool_handler/web/web_extract.py apps/backend/app/tools/tool_system.py apps/backend/tests/test_web_extract_tool.py apps/backend/tests/test_tool_system_web_tools.py
git commit -m "feat: add web extract tool"
```

## Task 7: End-To-End Tool Scheduler Integration

**Files:**
- Modify: `apps/backend/tests/test_tool_system_web_tools.py`

**Interfaces:**
- Consumes: `ToolSystem.build_tool_system()`, `ToolScheduler`, `ToolExecutionContext`.
- Produces: regression coverage that Web tools execute through the same scheduler path as existing tools.

- [ ] **Step 1: Write scheduler integration tests**

```python
import json
from pathlib import Path

import pytest

from app.tools.schemas.tool_execution_context import ToolExecutionContext
from app.tools.tool_system import ToolSystem


@pytest.mark.asyncio
async def test_web_search_executes_through_scheduler(monkeypatch, tmp_path: Path):
    system = ToolSystem.build_tool_system()
    context = ToolExecutionContext("task_1", "workspace_1", tmp_path)

    async def fake_execute_tool(tool_name, arguments, allowed_tool_names, execution_context):
        definition = system.registry.get_tool_definition(tool_name)
        return definition.handler(**arguments, execution_context=execution_context)

    observation = await fake_execute_tool(
        "web_search",
        {"query": "python", "limit": 1},
        {"web_search"},
        context,
    )

    assert observation.tool_name == "web_search"
    assert observation.status in {"success", "error"}
    assert observation.permission == "network"
```

- [ ] **Step 2: Run tests to verify current behavior**

Run: `cd apps/backend && uv run pytest tests/test_tool_system_web_tools.py -v`

Expected: PASS after Tasks 5 and 6. If this fails because of provider availability, update the test to inject a fake registry into `build_web_search_definition(provider_registry=registry)` and keep no-network behavior.

- [ ] **Step 3: Add display summaries**

Add `render_request_summary()` and `render_result_summary()` to both handlers:

- `web_search`: request summary is the query; result summary is a list of title/url rows.
- `web_extract`: request summary is the first URL plus count; result summary is a list of title/url/stored_path rows.

Use the existing `SearchFilesTool` display shape:

```python
{
    "kind": "web_result",
    "icon": "globe",
    "name": item["title"],
    "path": item["url"],
}
```

- [ ] **Step 4: Run focused and full backend tests**

Run:

```bash
cd apps/backend
uv run pytest tests/test_web_provider_registry.py tests/test_web_url_safety.py tests/test_web_content_store.py tests/test_web_providers.py tests/test_web_search_tool.py tests/test_web_extract_tool.py tests/test_tool_system_web_tools.py -v
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/tests/test_tool_system_web_tools.py apps/backend/app/tools/tool_handler/web
git commit -m "test: cover web tools scheduler integration"
```

## Task 8: Documentation And Final Verification

**Files:**
- Create: `docs/web-tools.md`
- Modify: `docs/superpowers/plans/2026-08-02-web-tools-implementation.md` only to check off completed implementation steps during execution.

**Interfaces:**
- Consumes: completed Web tool implementation.
- Produces: user-facing developer documentation for configuration and behavior.

- [ ] **Step 1: Write Web tools documentation**

Create `docs/web-tools.md` with these sections:

```markdown
# Web Tools

## Tools

`web_search` searches the Web and returns result metadata only.

`web_extract` reads specific URLs and returns cleaned page content. It does not summarize with an LLM.

## Configuration

Set one shared backend:

```env
CODING_AGENT_WEB_BACKEND=tavily
```

Or set per-capability backends:

```env
CODING_AGENT_WEB_SEARCH_BACKEND=brave-free
CODING_AGENT_WEB_EXTRACT_BACKEND=firecrawl
```

Supported backend names:

- `firecrawl`
- `parallel`
- `tavily`
- `exa`
- `searxng`
- `brave-free`
- `ddgs`

## Safety

`web_extract` blocks URLs with embedded secrets, credential-like query parameters, non-HTTP(S) schemes, and private/internal network targets before any provider request.

## Large Pages

Large extracted pages are truncated in the tool response. Full content is saved under `.coding-agent/tool-results/web/` inside the workspace and can be inspected with `read_file`.
```
```

- [ ] **Step 2: Run formatting and checks**

Run:

```bash
cd apps/backend
uv run ruff format app tests
uv run ruff check app tests
uv run mypy app
uv run pytest -q
```

Expected: all pass.

- [ ] **Step 3: Inspect dependencies**

Run:

```bash
git diff -- apps/backend/pyproject.toml apps/backend/uv.lock
```

Expected: no dependency changes unless `ddgs` was intentionally added. If `ddgs` was added, both `pyproject.toml` and `uv.lock` are updated together.

- [ ] **Step 4: Final commit**

```bash
git add docs/web-tools.md docs/superpowers/plans/2026-08-02-web-tools-implementation.md
git commit -m "docs: document web tools"
```

## Self-Review Checklist

- Spec coverage: This plan covers `web_search`, `web_extract`, Hermes-style provider routing, URL safety, output truncation, file storage, ToolSystem registration, tests, and docs.
- Scope check: Python execution is intentionally excluded.
- Architecture check: No API layer or service layer dependency is introduced; all Web behavior lives inside the tools layer.
- Registry check: No Hermes module-level tool registry is introduced.
- Provider check: Search-only providers are not used for extract.
- Safety check: URL safety happens before provider I/O.
- Test check: Unit tests use fakes or monkeypatches and do not perform live network calls.
- Type consistency: Later tasks use the provider and store signatures defined in Tasks 1-3.
- Placeholder scan: No unresolved placeholder wording or vague future-work markers remain.
