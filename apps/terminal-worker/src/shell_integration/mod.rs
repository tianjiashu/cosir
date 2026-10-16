//! shell 适配随 worker 打包；临时启动文件归本 worker 所有。

mod parser;
pub use parser::ShellParser;

use std::path::Path;
use tempfile::TempDir;

pub struct ShellLaunch {
    pub argv: Vec<String>,
    pub env: Vec<(String, String)>,
    pub token: String,
    pub unavailable: Option<&'static str>,
    // 保持文件活到 shell 退出，随后自动清理。
    _directory: Option<TempDir>,
}

impl ShellLaunch {
    pub fn prepare(argv: Vec<String>, kind: &str, integration: &str) -> Self {
        match Self::try_prepare(argv.clone(), kind, integration) {
            Ok(launch) => launch,
            Err(error) => {
                // stderr 由 Python worker client 收入结构化落盘日志；观察装配失败不阻断 PTY。
                eprintln!("shell integration setup failed: {error:#}");
                Self {
                    argv,
                    env: vec![],
                    token: String::new(),
                    unavailable: Some("setup_failed"),
                    _directory: None,
                }
            }
        }
    }

    fn try_prepare(argv: Vec<String>, kind: &str, integration: &str) -> anyhow::Result<Self> {
        let mut launch = Self {
            argv,
            env: vec![],
            token: String::new(),
            unavailable: None,
            _directory: None,
        };
        if integration == "off" {
            return Ok(launch);
        }
        if !matches!(kind, "zsh" | "powershell" | "pwsh") {
            launch.unavailable = Some("unsupported_shell");
            return Ok(launch);
        }
        let directory = tempfile::Builder::new().prefix("cosir-shell-").tempdir()?;
        launch.token = directory
            .path()
            .file_name()
            .unwrap()
            .to_string_lossy()
            .into_owned();
        launch
            .env
            .push(("COSIR_INTEGRATION_TOKEN".into(), launch.token.clone()));
        if kind == "zsh" {
            let path = directory.path();
            std::fs::write(
                path.join(".zshenv"),
                include_str!("../../resources/shell_integration/zsh/.zshenv"),
            )?;
            std::fs::write(
                path.join(".zshrc"),
                include_str!("../../resources/shell_integration/zsh/.zshrc"),
            )?;
            std::fs::write(
                path.join("integration.zsh"),
                include_str!("../../resources/shell_integration/zsh/integration.zsh"),
            )?;
            for name in [".zprofile", ".zlogin"] {
                std::fs::write(path.join(name), format!("ZDOTDIR=\"$__cosir_user_dir\"\n[[ -f \"$ZDOTDIR/{name}\" ]] && source \"$ZDOTDIR/{name}\"\n{}\n", if name == ".zprofile" { "__cosir_user_dir=\"$ZDOTDIR\"; ZDOTDIR=\"$__cosir_boot_dir\"" } else { ":" }))?;
            }
            let user_dir = std::env::var("ZDOTDIR").or_else(|_| std::env::var("HOME"))?;
            launch.env.push(("COSIR_USER_ZDOTDIR".into(), user_dir));
            launch
                .env
                .push(("ZDOTDIR".into(), path.to_string_lossy().into_owned()));
            launch.argv.push("-i".into());
        } else {
            let script = directory.path().join("integration.ps1");
            std::fs::write(
                &script,
                include_str!("../../resources/shell_integration/integration.ps1"),
            )?;
            // -Command 是启动装配，不包装后续输入；不修改机器执行策略或永久 profile。
            launch.argv.extend([
                "-NoExit".into(),
                "-Command".into(),
                format!(
                    ". ([scriptblock]::Create([IO.File]::ReadAllText('{}')))",
                    quote_ps_path(&script)
                ),
            ]);
        }
        launch._directory = Some(directory);
        Ok(launch)
    }
}

fn quote_ps_path(path: &Path) -> String {
    path.to_string_lossy().replace('\'', "''")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn powershell_bootstrap_preserves_profile_and_interactive_process() {
        let launch = ShellLaunch::prepare(
            vec!["powershell.exe".into(), "-NoLogo".into()],
            "powershell",
            "auto",
        );
        assert!(launch.argv.contains(&"-NoExit".into()));
        assert!(!launch.argv.contains(&"-NoProfile".into()));
        assert!(!launch.argv.contains(&"-ExecutionPolicy".into()));
        assert!(launch
            .argv
            .last()
            .unwrap()
            .contains("[scriptblock]::Create"));
        assert!(launch
            ._directory
            .as_ref()
            .unwrap()
            .path()
            .join("integration.ps1")
            .is_file());
    }

    #[test]
    fn disabled_and_unsupported_shells_preserve_original_argv() {
        let argv = vec!["shell".into(), "argument".into()];
        let off = ShellLaunch::prepare(argv.clone(), "zsh", "off");
        assert_eq!(off.argv, argv);
        assert!(off.env.is_empty());
        let other = ShellLaunch::prepare(argv.clone(), "fish", "auto");
        assert_eq!(other.argv, argv);
        assert_eq!(other.unavailable, Some("unsupported_shell"));
    }

    #[test]
    fn script_directory_lives_until_launch_is_dropped() {
        let launch = ShellLaunch::prepare(vec!["pwsh".into()], "pwsh", "auto");
        let path = launch._directory.as_ref().unwrap().path().to_owned();
        drop(launch);
        assert!(!path.exists());
        assert_eq!(quote_ps_path(Path::new("a'b.ps1")), "a''b.ps1");
    }
}
