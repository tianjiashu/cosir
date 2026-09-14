use std::fs;

const MAX_ATTACHMENT_BYTES: u64 = 32 * 1024 * 1024;

/// Canonicalize a user-selected regular file without reading its contents.
#[tauri::command]
pub fn resolve_selected_attachment_path(path: String) -> Result<String, String> {
    let selected = fs::canonicalize(&path).map_err(|_| "所选文件不可用".to_string())?;
    if !selected.is_file() {
        return Err("所选路径不是文件".to_string());
    }
    Ok(selected.to_string_lossy().into_owned())
}

/// Read a user-selected local file for attachment staging.
///
/// The native dialog is the user-consent boundary. This command remains read-only
/// and bounded, validates the canonical target and regular-file status, and does
/// not impose a workspace boundary: the workspace is only the picker's initial
/// directory, not the scope of files the user may explicitly attach.
#[tauri::command]
pub fn read_selected_attachment_file(path: String) -> Result<Vec<u8>, String> {
    let selected = fs::canonicalize(&path).map_err(|_| "所选文件不可用".to_string())?;
    if !selected.is_file() {
        return Err("所选路径不是文件".to_string());
    }

    let metadata = fs::metadata(&selected).map_err(|_| "无法读取所选文件".to_string())?;
    if metadata.len() > MAX_ATTACHMENT_BYTES {
        return Err("附件文件过大".to_string());
    }
    fs::read(&selected).map_err(|_| "无法读取所选文件".to_string())
}
