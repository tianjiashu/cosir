//! 终端尺寸：PTY、屏幕内核与握手共用的唯一尺寸契约。
//!
//! 尺寸随人的面板变化（`resize` 帧），因此不再是编译期常量；取值范围在此一处校验，
//! PTY 与屏幕内核只接收已校验的 `Geometry`。

pub const MIN_ROWS: u16 = 2;
pub const MAX_ROWS: u16 = 500;
pub const MIN_COLS: u16 = 10;
pub const MAX_COLS: u16 = 1000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Geometry {
    pub rows: u16,
    pub cols: u16,
}

impl Geometry {
    /// 新建会话的初始尺寸；面板打开后由前端 resize 覆盖。
    pub const DEFAULT: Self = Self { rows: 24, cols: 80 };

    /// 校验 resize 请求；越界由调用方转为非致命 `resize_rejected`。
    pub fn new(rows: u16, cols: u16) -> anyhow::Result<Self> {
        if !(MIN_ROWS..=MAX_ROWS).contains(&rows) || !(MIN_COLS..=MAX_COLS).contains(&cols) {
            anyhow::bail!(
                "terminal size {rows}x{cols} out of range rows {MIN_ROWS}..={MAX_ROWS}, cols {MIN_COLS}..={MAX_COLS}"
            );
        }
        Ok(Self { rows, cols })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_inclusive_bounds_and_rejects_outside() {
        assert!(Geometry::new(MIN_ROWS, MIN_COLS).is_ok());
        assert!(Geometry::new(MAX_ROWS, MAX_COLS).is_ok());
        assert!(Geometry::new(MIN_ROWS - 1, 80).is_err());
        assert!(Geometry::new(MAX_ROWS + 1, 80).is_err());
        assert!(Geometry::new(24, MIN_COLS - 1).is_err());
        assert!(Geometry::new(24, MAX_COLS + 1).is_err());
    }
}
