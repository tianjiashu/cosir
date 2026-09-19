# Research Brief: Cosir 单文件 Windows 打包

**Depth**: focused  
**Date**: 2026-09-19

## Executive summary

当前项目已经完成主要的分层打包：TypeScript 构建为 Vite 静态资源，Rust 是 Tauri 宿主，Python 通过 PyInstaller `--onedir` 生成后端目录。当前产物总量约 128.7 MB，其中 Python backend 约 118.4 MB、terminal worker 约 0.5 MB、前端约 1.7 MB、Rust 主程序约 9.7 MB。

推荐先明确两个“单 exe”定义：

1. **单个安装包 exe**：使用 Tauri NSIS `*-setup.exe`，安装后仍是多文件。这是默认推荐方案，改动小、启动稳定、便于升级。
2. **真正的单文件便携 exe**：把 backend/terminal-worker 压缩包作为 Rust 编译输入嵌入主程序，首次运行解压到 `%LOCALAPPDATA%\\Cosir\\runtime\\<hash>` 后复用。这满足分发目录只有一个 exe，但无法避免首次解压。

## Local findings

- `apps/desktop/scripts/stage-backend.mjs` 使用 PyInstaller `--onedir`。
- `apps/desktop/src-tauri/tauri.conf.json` 通过 `bundle.resources` 旁挂 `backend/` 和 `terminal-worker/`。
- release backend runtime 从 `resource_dir/backend/cosir-backend.exe` 启动，因此不能只改 PyInstaller 参数而得到真正单文件。
- 当前 backend 目录中较大的文件包括 cryptography、Pillow AVIF、tokenizers 和 tree-sitter grammar；LiteLLM 已从运行时依赖中移除。
- `npm run build:bundle` 已经是前端、terminal worker、backend staging 的统一入口，适合扩展成发布流水线。

## Options

| 目标 | 方案 | 结果 | 推荐度 |
| --- | --- | --- | --- |
| 一个可下载 exe | Tauri NSIS installer | 一个 `Cosir_x64-setup.exe`，安装后展开资源 | 首选 |
| 一个便携 exe | Rust `include_bytes!`/build script 嵌入压缩 backend bundle | 分发只有 `Cosir.exe`，首次运行解压并缓存 | 有严格单文件要求时 |
| 一个便携 exe | PyInstaller `--onefile` + Tauri resource | 仍需要把 backend exe 作为旁挂资源，除非再做 Rust 嵌入 | 不推荐 |

## Recommended architecture for true single-file mode

```text
build frontend
  -> Vite dist embedded by Tauri
build Python backend
  -> PyInstaller onedir
  -> backend.bundle (compressed archive)
build terminal worker
  -> terminal-worker.bundle or binary
Cargo/Tauri build
  -> include bundle bytes in Rust binary
runtime
  -> verify hash
  -> atomic extract to %LOCALAPPDATA%/Cosir/runtime/<hash>
  -> start backend child process from extracted directory
```

Prefer embedding the existing PyInstaller **onedir** output as one compressed archive instead of switching Python directly to `--onefile`: PyInstaller onefile itself extracts to a temporary `_MEI*` directory and starts more slowly. Rust can cache the extracted onedir bundle by content hash, avoiding extraction on every launch.

The Rust side should own extraction, hash verification, cache cleanup and backend startup. The React frontend must remain unaware of the packaging layout and continue using the runtime config supplied by Tauri.

## Size reduction priorities

1. 已移除 LiteLLM 依赖、PyInstaller 收集配置和 frozen smoke check；后续体积优化应基于实际运行链路检查剩余可选 native 依赖。
2. Keep only the tree-sitter language grammars supported by the product.
3. Re-evaluate Pillow AVIF and other optional native codecs against actual image requirements.
4. Use a release profile with symbol stripping, Thin LTO and size-oriented optimization; benchmark startup before enabling more aggressive settings.
5. Build one architecture per artifact (`x86_64-pc-windows-msvc` or `aarch64-pc-windows-msvc`), not a combined multi-architecture payload.
6. Do not bundle the offline/fixed WebView2 runtime unless offline installation is required; the offline installer adds about 127 MB and fixed-version mode about 180 MB according to Tauri's documentation.

## Sources

- [Tauri Windows Installer](https://tauri.app/distribute/windows-installer/)
- [Tauri Embedding External Binaries](https://v2.tauri.app/develop/sidecar/)
- [Tauri Configuration: resources and externalBin](https://v2.tauri.app/reference/config/)
- [PyInstaller operating modes](https://pyinstaller.org/en/stable/operating-mode.html)
- [Rust `include_bytes!`](https://doc.rust-lang.org/std/macro.include_bytes.html)
- [Cargo build scripts](https://doc.rust-lang.org/cargo/reference/build-scripts.html)
