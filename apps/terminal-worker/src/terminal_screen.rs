//! 无界面终端状态：复用 Alacritty 执行控制序列，业务命令边界仍归 shell integration。
use std::sync::mpsc::{self, Receiver, Sender};

use alacritty_terminal::event::{Event, EventListener, WindowSize};
use alacritty_terminal::grid::Dimensions;
use alacritty_terminal::index::{Column, Line};
use alacritty_terminal::term::cell::Flags;
use alacritty_terminal::term::{Config, Osc52, TermMode};
use alacritty_terminal::vte::ansi::Processor;
use alacritty_terminal::Term;
use serde::Serialize;

pub const ROWS: u16 = 24;
pub const COLS: u16 = 80;

struct Size;
impl Dimensions for Size {
    fn total_lines(&self) -> usize {
        ROWS as usize
    }
    fn screen_lines(&self) -> usize {
        ROWS as usize
    }
    fn columns(&self) -> usize {
        COLS as usize
    }
}

struct Listener(Sender<String>);
impl EventListener for Listener {
    fn send_event(&self, event: Event) {
        let reply = match event {
            Event::PtyWrite(text) => text,
            Event::TextAreaSizeRequest(format) => format(WindowSize {
                num_lines: ROWS,
                num_cols: COLS,
                cell_width: 0,
                cell_height: 0,
            }),
            // 无窗口、剪贴板或配色查询能力；这些宿主请求不影响屏幕事实。
            _ => return,
        };
        let _ = self.0.send(reply);
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
    pub visible: bool,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ScreenSnapshot {
    pub revision: u64,
    pub rows: u16,
    pub columns: u16,
    pub lines: Vec<ScreenRow>,
    pub cursor: ScreenCursor,
    pub alternate: bool,
}

/// 屏幕只有一份；滚出可见区的行立即移交后端有界历史，不做文本去重。
pub struct TerminalScreen {
    parser: Processor,
    terminal: Term<Listener>,
    replies: Receiver<String>,
    history: Vec<String>,
    previous: Option<ScreenSnapshot>,
}
impl TerminalScreen {
    pub fn new() -> Self {
        let (tx, rx) = mpsc::channel();
        let config = Config {
            scrolling_history: ROWS as usize,
            osc52: Osc52::Disabled,
            ..Config::default()
        };
        Self {
            parser: Processor::new(),
            terminal: Term::new(config, &Size, Listener(tx)),
            replies: rx,
            history: Vec::new(),
            previous: None,
        }
    }

    /// 逐字节推进后导出历史，避免同一块内的清屏/切换屏幕抹去尚未移交的行。
    pub fn advance(&mut self, byte: u8) {
        self.parser.advance(&mut self.terminal, &[byte]);
        // 无界面观察不延迟同步更新；逐字节导出确保批次内的历史也不被库容量淘汰。
        if self.parser.sync_timeout().sync_timeout().is_some() {
            self.parser.stop_sync(&mut self.terminal);
        }
        self.drain_history();
    }

    fn drain_history(&mut self) {
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
        let mut text = String::new();
        for col in 0..COLS as usize {
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
        let wrapped = row[Column(COLS as usize - 1)]
            .flags
            .contains(Flags::WRAPLINE);
        if !wrapped {
            text.truncate(text.trim_end_matches(' ').len());
        }
        ScreenRow { text, wrapped }
    }

    pub fn take_replies(&self) -> Vec<String> {
        self.replies.try_iter().collect()
    }

    /// 观察边界发布完整替换快照；版本只因投影变化推进，历史另行追加。
    pub fn take_update(&mut self) -> Option<(ScreenSnapshot, Vec<String>)> {
        let point = self.terminal.grid().cursor.point;
        let mut screen = ScreenSnapshot {
            revision: self.previous.as_ref().map_or(0, |s| s.revision),
            rows: ROWS,
            columns: COLS,
            lines: (0..ROWS as i32).map(|row| self.row(row)).collect(),
            cursor: ScreenCursor {
                row: point.line.0 as usize,
                column: point.column.0,
                visible: self.terminal.mode().contains(TermMode::SHOW_CURSOR),
            },
            alternate: self.terminal.mode().contains(TermMode::ALT_SCREEN),
        };
        let changed = self.previous.as_ref() != Some(&screen);
        if changed {
            screen.revision += 1;
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
        let mut s = TerminalScreen::new();
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
        let mut s = TerminalScreen::new();
        feed(&mut s, "same\r\n".repeat(200).as_bytes());
        let (view, history) = s.take_update().unwrap();
        assert_eq!(history.iter().filter(|s| *s == "same\n").count(), 177);
        assert_eq!(view.lines.iter().filter(|r| r.text == "same").count(), 23);
    }
    #[test]
    fn synchronized_updates_do_not_hide_prompts_or_overflow_history() {
        let mut s = TerminalScreen::new();
        feed(&mut s, b"\x1b[?2026h");
        feed(&mut s, "same\r\n".repeat(200).as_bytes());
        // 即使程序漏发结束标记，当前画面和历史也可被观察。
        let (view, history) = s.take_update().unwrap();
        assert_eq!(history.len(), 177);
        assert_eq!(view.lines.iter().filter(|r| r.text == "same").count(), 23);
    }

    #[test]
    fn alternate_screen_and_scroll_region_do_not_append_fake_history() {
        let mut s = TerminalScreen::new();
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
        let mut s = TerminalScreen::new();
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
        let mut whole = TerminalScreen::new();
        feed(&mut whole, &bytes);
        let expected = whole.take_update().unwrap().0;
        for split in 0..=bytes.len() {
            let mut s = TerminalScreen::new();
            feed(&mut s, &bytes[..split]);
            s.take_update();
            feed(&mut s, &bytes[split..]);
            let mut actual = s
                .take_update()
                .map(|v| v.0)
                .unwrap_or_else(|| s.previous.clone().unwrap());
            actual.revision = expected.revision;
            assert_eq!(actual, expected, "split {split}");
        }
    }
}
