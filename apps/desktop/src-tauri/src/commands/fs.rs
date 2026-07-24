//! 文件系统相关 IPC 命令。
//!
//! 承载桌面端面向「本地文件」的能力，例如用系统默认应用打开文件（由工具卡片
//! 的「打开文件」动作触发）。命令只做最小跨平台打开，不引入额外 crate。

use std::path::Path;
use std::process::Command;

/// 用系统默认应用打开指定文件。
///
/// 参数:
///     path: 待打开文件的路径（前端应传入已解析为可直接打开的路径）。
///
/// 返回:
///     `Ok(())` 表示已成功拉起系统打开动作。
///
/// 异常:
///     当文件不存在或无法拉起系统打开命令时返回错误字符串。
///
/// 副作用:
///     可能拉起系统默认应用（macOS `open` / Linux `xdg-open` / Windows `explorer`）。
#[tauri::command]
pub fn open_file_in_editor(path: String) -> Result<(), String> {
    let target = Path::new(&path);
    if !target.exists() {
        return Err(format!("文件不存在: {path}"));
    }

    let opener = if cfg!(target_os = "windows") {
        "explorer"
    } else if cfg!(target_os = "macos") {
        "open"
    } else {
        "xdg-open"
    };

    Command::new(opener)
        .arg(target)
        .spawn()
        .map_err(|err| format!("打开文件失败 ({opener}): {err}"))?;

    Ok(())
}
