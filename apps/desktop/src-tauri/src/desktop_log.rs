use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::Path;
use std::sync::{Mutex, OnceLock};

const MAX_BYTES: u64 = 10 * 1024 * 1024;
const BACKUP_COUNT: u32 = 7;

pub fn append_json_line(path: &Path, mut entry: serde_json::Value) -> Result<(), String> {
    let lock = log_lock(path);
    let _guard = lock.lock().map_err(|_| "日志写入锁已损坏".to_string())?;
    append_json_line_locked(path, &mut entry)
}

pub fn append_console_line(path: &Path, stream: &str, line: &str) -> Result<(), String> {
    append_json_line(
        path,
        serde_json::json!({
            "level": "INFO",
            "logger": "coding_agent.backend_console",
            "trace_id": "",
            "caller": "backend_process",
            "event": "backend_console_output",
            "msg": line,
            "data": {"stream": stream},
            "error": null,
            "truncated": false
        }),
    )
}

fn append_json_line_locked(path: &Path, entry: &mut serde_json::Value) -> Result<(), String> {
    let object = entry
        .as_object_mut()
        .ok_or_else(|| "日志必须是 JSON 对象".to_string())?;
    object
        .entry("ts".to_string())
        .or_insert_with(|| serde_json::Value::String(timestamp()));
    let line = serde_json::to_string(&entry).map_err(|error| format!("日志序列化失败：{error}"))?;
    if line.len() as u64 > MAX_BYTES {
        return Err("日志单行超过大小上限".to_string());
    }
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| format!("无法创建日志目录：{error}"))?;
    }
    rotate_if_needed(path)?;
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|error| format!("无法打开日志文件：{error}"))?;
    writeln!(file, "{line}").map_err(|error| format!("无法写入日志：{error}"))
}

fn rotate_if_needed(path: &Path) -> Result<(), String> {
    let too_large = fs::metadata(path)
        .map(|metadata| metadata.len() >= MAX_BYTES)
        .unwrap_or(false);
    if !too_large {
        return Ok(());
    }
    let oldest = path.with_extension(format!("log.{BACKUP_COUNT}"));
    let _ = fs::remove_file(oldest);
    for index in (1..BACKUP_COUNT).rev() {
        let source = path.with_extension(format!("log.{index}"));
        let target = path.with_extension(format!("log.{}", index + 1));
        if source.exists() {
            let _ = fs::remove_file(&target);
            fs::rename(source, target).map_err(|error| format!("日志轮转失败：{error}"))?;
        }
    }
    let first = path.with_extension("log.1");
    let _ = fs::remove_file(&first);
    fs::rename(path, first).map_err(|error| format!("日志轮转失败：{error}"))
}

fn log_lock(path: &Path) -> &'static Mutex<()> {
    static DESKTOP_LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    static FRONTEND_LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    if path.file_name().and_then(|name| name.to_str()) == Some("frontend.log") {
        FRONTEND_LOCK.get_or_init(|| Mutex::new(()))
    } else {
        DESKTOP_LOCK.get_or_init(|| Mutex::new(()))
    }
}

fn timestamp() -> String {
    time::OffsetDateTime::now_utc()
        .format(&time::format_description::well_known::Rfc3339)
        .unwrap_or_else(|_| "1970-01-01T00:00:00Z".to_string())
}
