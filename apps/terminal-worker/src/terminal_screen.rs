//! 无界面终端状态：复用 Alacritty 执行控制序列，业务命令边界仍归 shell integration。
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::Arc;

use alacritty_terminal::event::{Event, EventListener, WindowSize};
use alacritty_terminal::grid::Dimensions;
use alacritty_terminal::index::{Column, Line};
use alacritty_terminal::term::cell::Flags;
use alacritty_terminal::term::{Config, Osc52, TermMode};
use alacritty_terminal::vte::ansi::Processor;
use alacritty_terminal::Term;
use serde::Serialize;

use crate::geometry::{Geometry, MAX_ROWS};

struct Size(Geometry);
impl Dimensions for Size {
    fn total_lines(&self) -> usize {
        self.0.rows as usize
    }
    fn screen_lines(&self) -> usize {
        self.0.rows as usize
    }
    fn columns(&self) -> usize {
        self.0.cols as usize
    }
}

/// 尺寸查询（`CSI 18 t`）在 Term 内部回调里应答，拿不到 `&self`；
/// 以原子量与 resize 同步，避免回报旧尺寸。
fn pack(geometry: Geometry) -> u32 {
    (u32::from(geometry.rows) << 16) | u32::from(geometry.cols)
}

struct Listener {
    replies: Sender<String>,
    geometry: Arc<AtomicU32>,
}
impl EventListener for Listener {
    fn send_event(&self, event: Event) {
        let reply = match event {
            Event::PtyWrite(text) => text,
            Event::TextAreaSizeRequest(format) => {
                let packed = self.geometry.load(Ordering::Relaxed);
                format(WindowSize {
                    num_lines: (packed >> 16) as u16,
                    num_cols: (packed & 0xffff) as u16,
                    cell_width: 0,
                    cell_height: 0,
                })
            }
            // 无窗口、剪贴板或配色查询能力；这些宿主请求不影响屏幕事实。
            _ => return,
        };
        let _ = self.replies.send(reply);
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ScreenRow {
    pub text: String,
    pub wrapped: bool,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ScreenCursor {
    pub row: usize,
    pub column: usize,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ScreenSnapshot {
    pub rows: u16,
    pub columns: u16,
    pub lines: Vec<ScreenRow>,
    pub cursor: ScreenCursor,
    pub alternate: bool,
}

/// 屏幕只有一份；滚出可见区的行立即移交后端有界历史，不做文本去重。
///
/// 读线程推进输出、控制线程执行 resize，二者经 worker 持有的同一把锁串行，
/// 保证 resize 产生的历史与新尺寸画面不会插进同一输出块的中间。
pub struct TerminalScreen {
    parser: Processor,
    terminal: Term<Listener>,
    replies: Receiver<String>,
    history: Vec<String>,
    previous: Option<ScreenSnapshot>,
    geometry: Arc<AtomicU32>,
    app_cursor: Arc<AtomicBool>,
}
impl TerminalScreen {
    pub fn new(geometry: Geometry) -> Self {
        let (tx, rx) = mpsc::channel();
        let shared_geometry = Arc::new(AtomicU32::new(pack(geometry)));
        let config = Config {
            // 历史每字节即被移交，库内容量只需兜住单次 resize 缩行：最多推出 MAX_ROWS 行。
            scrolling_history: MAX_ROWS as usize,
            osc52: Osc52::Disabled,
            ..Config::default()
        };
        let listener = Listener {
            replies: tx,
            geometry: shared_geometry.clone(),
        };
        Self {
            parser: Processor::new(),
            terminal: Term::new(config, &Size(geometry), listener),
            replies: rx,
            history: Vec::new(),
            previous: None,
            geometry: shared_geometry,
            app_cursor: Arc::new(AtomicBool::new(false)),
        }
    }

    /// 光标键模式（DECCKM）的只读视图：控制线程编码方向键时无锁读取，
    /// 不必为一次按键等待读线程处理完整个输出块。
    pub fn app_cursor_flag(&self) -> Arc<AtomicBool> {
        self.app_cursor.clone()
    }

    /// 逐字节推进后导出历史，避免同一块内的清屏/切换屏幕抹去尚未移交的行。
    pub fn advance(&mut self, byte: u8) {
        self.parser.advance(&mut self.terminal, &[byte]);
        // 无界面观察不延迟同步更新；逐字节导出确保批次内的历史也不被库容量淘汰。
        if self.parser.sync_timeout().sync_timeout().is_some() {
            self.parser.stop_sync(&mut self.terminal);
        }
        self.app_cursor.store(
            self.terminal.mode().contains(TermMode::APP_CURSOR),
            Ordering::Relaxed,
        );
        self.drain_history();
    }

    /// 只调整屏幕内核；缩行推出的行留在库历史里，由随后的 `drain_history` 移交。
    pub fn resize(&mut self, geometry: Geometry) {
        self.terminal.resize(Size(geometry));
        self.geometry.store(pack(geometry), Ordering::Relaxed);
    }

    pub fn drain_history(&mut self) {
        let count = self.terminal.grid().history_size() as i32;
        for index in -count..0 {
            let row = self.row(index);
            self.history.push(if row.wrapped {
                row.text
            } else {
                row.text + "\n"
            });
        }
        if count > 0 {
            self.terminal.grid_mut().clear_history();
        }
    }

    fn row(&self, index: i32) -> ScreenRow {
        let row = &self.terminal.grid()[Line(index)];
        let columns = self.terminal.columns();
        let mut text = String::new();
        for col in 0..columns {
            let cell = &row[Column(col)];
            if cell
                .flags
                .intersects(Flags::WIDE_CHAR_SPACER | Flags::LEADING_WIDE_CHAR_SPACER)
            {
                continue;
            }
            text.push(cell.c);
            if let Some(marks) = cell.zerowidth() {
                text.extend(marks);
            }
        }
        let wrapped = row[Column(columns - 1)].flags.contains(Flags::WRAPLINE);
        if !wrapped {
            text.truncate(text.trim_end_matches(' ').len());
        }
        ScreenRow { text, wrapped }
    }

    pub fn take_replies(&self) -> Vec<String> {
        self.replies.try_iter().collect()
    }

    /// 观察边界发布完整替换快照；只在投影变化或有新历史时发布，历史另行追加。
    pub fn take_update(&mut self) -> Option<(ScreenSnapshot, Vec<String>)> {
        let point = self.terminal.grid().cursor.point;
        let screen = ScreenSnapshot {
            rows: self.terminal.screen_lines() as u16,
            columns: self.terminal.columns() as u16,
            lines: (0..self.terminal.screen_lines() as i32)
                .map(|row| self.row(row))
                .collect(),
            cursor: ScreenCursor {
                row: point.line.0 as usize,
                column: point.column.0,
            },
            alternate: self.terminal.mode().contains(TermMode::ALT_SCREEN),
        };
        let changed = self.previous.as_ref() != Some(&screen);
        if changed {
            self.previous = Some(screen.clone());
        }
        if changed || !self.history.is_empty() {
            Some((screen, std::mem::take(&mut self.history)))
        } else {
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn feed(s: &mut TerminalScreen, bytes: &[u8]) {
        for byte in bytes {
            s.advance(*byte);
        }
    }
    #[test]
    fn redraw_unicode_and_queries_use_the_same_grid() {
        let mut s = TerminalScreen::new(Geometry::DEFAULT);
        feed(&mut s, "abc\rxy\x1b[K\r\n中e\u{301}\x1b[6n".as_bytes());
        let (view, history) = s.take_update().unwrap();
        assert_eq!(view.lines[0].text, "xy");
        assert_eq!(view.lines[1].text, "中e\u{301}");
        assert_eq!(view.cursor.column, 3);
        assert!(history.is_empty());
        assert_eq!(s.take_replies(), vec!["\x1b[2;4R"]);
        assert!(s.take_update().is_none());
    }
    #[test]
    fn identical_history_is_never_deduplicated_or_lost_at_buffer_limit() {
        let mut s = TerminalScreen::new(Geometry::DEFAULT);
        feed(&mut s, "same\r\n".repeat(200).as_bytes());
        let (view, history) = s.take_update().unwrap();
        assert_eq!(history.iter().filter(|s| *s == "same\n").count(), 177);
        assert_eq!(view.lines.iter().filter(|r| r.text == "same").count(), 23);
    }
    #[test]
    fn synchronized_updates_do_not_hide_prompts_or_overflow_history() {
        let mut s = TerminalScreen::new(Geometry::DEFAULT);
        feed(&mut s, b"\x1b[?2026h");
        feed(&mut s, "same\r\n".repeat(200).as_bytes());
        // 即使程序漏发结束标记，当前画面和历史也可被观察。
        let (view, history) = s.take_update().unwrap();
        assert_eq!(history.len(), 177);
        assert_eq!(view.lines.iter().filter(|r| r.text == "same").count(), 23);
    }

    #[test]
    fn alternate_screen_and_scroll_region_do_not_append_fake_history() {
        let mut s = TerminalScreen::new(Geometry::DEFAULT);
        feed(&mut s, b"original\x1b[?1049h");
        feed(&mut s, "alternate\r\n".repeat(50).as_bytes());
        assert!(s.take_update().unwrap().0.alternate);
        feed(&mut s, b"\x1b[?1049l");
        let (view, history) = s.take_update().unwrap();
        assert!(!view.alternate);
        assert_eq!(view.lines[0].text, "original");
        assert!(history.is_empty());
        feed(&mut s, b"\x1b[2;4r\x1b[4;1H");
        feed(&mut s, "region\r\n".repeat(10).as_bytes());
        assert!(s.take_update().unwrap().1.is_empty());
    }
    #[test]
    fn powershell_style_position_erase_and_dsr_sequences() {
        // 合成控制序列验证，不冒充 Windows/PSReadLine 真实录制。
        let mut s = TerminalScreen::new(Geometry::DEFAULT);
        feed(
            &mut s,
            b"\x1b[2J\x1b[HPS> Write-Output old\x1b[1;5HWrite-Output new\x1b[K\x1b[6n",
        );
        assert_eq!(s.take_replies(), vec!["\x1b[1;21R"]);
        assert_eq!(
            s.take_update().unwrap().0.lines[0].text,
            "PS> Write-Output new"
        );
    }
    #[test]
    fn long_wrap_and_escape_sequences_survive_every_chunk_boundary() {
        let bytes = format!(
            "{}中e\u{301}\x1b]633;Cosir;t;shell_prompt;/tmp\x07\r\nTAIL []",
            "a".repeat(159)
        )
        .into_bytes();
        let mut whole = TerminalScreen::new(Geometry::DEFAULT);
        feed(&mut whole, &bytes);
        let expected = whole.take_update().unwrap().0;
        for split in 0..=bytes.len() {
            let mut s = TerminalScreen::new(Geometry::DEFAULT);
            feed(&mut s, &bytes[..split]);
            s.take_update();
            feed(&mut s, &bytes[split..]);
            let actual = s
                .take_update()
                .map(|v| v.0)
                .unwrap_or_else(|| s.previous.clone().unwrap());
            assert_eq!(actual, expected, "split {split}");
        }
    }

    #[test]
    fn shrinking_rows_hands_every_pushed_line_to_history() {
        let mut s = TerminalScreen::new(Geometry::new(MAX_ROWS, 80).unwrap());
        let text: String = (0..MAX_ROWS - 1).map(|i| format!("line{i}\r\n")).collect();
        feed(&mut s, text.as_bytes());
        let (_, history) = s.take_update().unwrap();
        assert!(history.is_empty());
        s.resize(Geometry::new(2, 80).unwrap());
        s.drain_history();
        let (view, history) = s.take_update().unwrap();
        assert_eq!((view.rows, view.columns), (2, 80));
        // 光标在末行空提示行：缩到 2 行时只留最后一行内容与光标行，其余全部进入历史。
        assert_eq!(history.len(), MAX_ROWS as usize - 2);
        assert_eq!(history[0], "line0\n");
        assert_eq!(history.last().unwrap(), &format!("line{}\n", MAX_ROWS - 3));
        assert_eq!(view.lines[0].text, format!("line{}", MAX_ROWS - 2));
    }

    #[test]
    fn resize_changes_snapshot_width_and_size_query() {
        let mut s = TerminalScreen::new(Geometry::DEFAULT);
        s.resize(Geometry::new(40, 120).unwrap());
        feed(&mut s, &[b'x'; 100]);
        feed(&mut s, b"\x1b[18t");
        let (view, _) = s.take_update().unwrap();
        assert_eq!((view.rows, view.columns), (40, 120));
        assert_eq!(view.lines.len(), 40);
        assert_eq!(view.lines[0].text, "x".repeat(100));
        assert_eq!(s.take_replies(), vec!["\x1b[8;40;120t"]);
    }

    #[test]
    fn app_cursor_flag_follows_decckm() {
        let mut s = TerminalScreen::new(Geometry::DEFAULT);
        let flag = s.app_cursor_flag();
        assert!(!flag.load(Ordering::Relaxed));
        feed(&mut s, b"\x1b[?1h");
        assert!(flag.load(Ordering::Relaxed));
        feed(&mut s, b"\x1b[?1l");
        assert!(!flag.load(Ordering::Relaxed));
    }
}
