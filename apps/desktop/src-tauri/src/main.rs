//! Tauri 2 桌面二进制入口。
//!
//! 仅作为薄封装调用库入口 `coding_agent_desktop_lib::run()`，
//! 便于后续移动端复用同一套运行时逻辑。

fn main() {
    coding_agent_desktop_lib::run();
}
