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
        if !matches!(kind, "zsh" | "bash" | "powershell" | "pwsh") {
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
        } else if kind == "bash" {
            let path = directory.path();
            let rcfile = path.join("bashrc");
            std::fs::write(
                &rcfile,
                include_str!("../../resources/shell_integration/bash/bashrc"),
            )?;
            std::fs::write(
                path.join("integration.bash"),
                include_str!("../../resources/shell_integration/bash/integration.bash"),
            )?;
            launch.env.push((
                "COSIR_BASH_BOOT_DIR".into(),
                path.to_string_lossy().into_owned(),
            ));
            // bash 只在单字母选项之前识别长选项，故紧跟可执行文件插入。
            // 不用 login 模式：login shell 忽略 --rcfile；用户 login 链由 bashrc 按平台补回。
            launch.argv.splice(
                1..1,
                [
                    "--rcfile".to_owned(),
                    rcfile.to_string_lossy().into_owned(),
                    "-i".to_owned(),
                ],
            );
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
    fn bash_bootstrap_injects_rcfile_before_other_arguments() {
        let launch = ShellLaunch::prepare(
            vec!["/bin/bash".into(), "-o".into(), "vi".into()],
            "bash",
            "auto",
        );
        assert!(launch.unavailable.is_none());
        let directory = launch._directory.as_ref().unwrap().path();
        let rcfile = directory.join("bashrc");
        assert_eq!(
            launch.argv,
            vec![
                "/bin/bash".to_owned(),
                "--rcfile".to_owned(),
                rcfile.to_string_lossy().into_owned(),
                "-i".to_owned(),
                "-o".to_owned(),
                "vi".to_owned(),
            ]
        );
        assert!(rcfile.is_file());
        assert!(directory.join("integration.bash").is_file());
        let env: std::collections::HashMap<_, _> = launch.env.iter().cloned().collect();
        assert_eq!(env["COSIR_INTEGRATION_TOKEN"], launch.token);
        assert_eq!(
            env["COSIR_BASH_BOOT_DIR"],
            directory.to_string_lossy().into_owned()
        );
        // 一次性身份必须先于用户配置被取走并移出环境。
        let rc = std::fs::read_to_string(&rcfile).unwrap();
        let unset = rc.find("unset COSIR_INTEGRATION_TOKEN").unwrap();
        assert!(unset < rc.find("/etc/profile").unwrap());
        assert!(unset < rc.find(".bashrc").unwrap());
        let source = rc.find(". \"$__cosir_boot_dir/integration.bash\"").unwrap();
        assert!(source < rc.find("builtin trap").unwrap());
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
