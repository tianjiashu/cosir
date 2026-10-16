# 根交互读循环是命令边界；Prompt 仅捕获状态，避免手工调用 prompt 伪造完成。
$global:__Cosir = @{
    Token = $env:COSIR_INTEGRATION_TOKEN
    OriginalPrompt = $function:prompt
    Active = $false
    Success = $null
    HistoryId = -1
}
Remove-Item Env:COSIR_INTEGRATION_TOKEN -ErrorAction SilentlyContinue
function global:__CosirEscape([string]$Value) {
    return $Value.Replace('\', '\\').Replace(';', '\x3b').Replace("`n", '\x0a').Replace("`r", '\x0d').Replace([string][char]27, '\x1b').Replace([string][char]7, '\x07')
}
function global:__CosirEmit([string]$Kind, [string]$Value) {
    [Console]::Write("$([char]27)]633;Cosir;$($global:__Cosir.Token);$Kind;$Value$([char]7)")
}
if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
    __CosirEmit 'unavailable' 'restricted_language_mode'
    return
}
try {
    Import-Module PSReadLine -ErrorAction Stop
} catch {
    __CosirEmit 'unavailable' 'psreadline_unavailable'
    return
}
$global:__Cosir.OriginalReadLine = $function:PSConsoleHostReadLine
function global:prompt {
    $global:__Cosir.Success = $global:?
    & $global:__Cosir.OriginalPrompt
}
function global:PSConsoleHostReadLine {
    if ($NestedPromptLevel -gt 0) {
        return (& $global:__Cosir.OriginalReadLine)
    }
    $history = Get-History -Count 1
    if ($global:__Cosir.Active) {
        $result = 'unknown'
        if ($null -ne $history -and $history.Id -ne $global:__Cosir.HistoryId) {
            $result = if ($global:__Cosir.Success) { 'success' } else { 'failure' }
        }
        __CosirEmit 'command_returned' $result
        $global:__Cosir.Active = $false
    }
    $global:__Cosir.HistoryId = if ($null -eq $history) { -1 } else { $history.Id }
    $directory = if ($pwd.Provider.Name -eq 'FileSystem') { $pwd.ProviderPath } else { '' }
    __CosirEmit 'shell_prompt' (__CosirEscape $directory)
    $line = & $global:__Cosir.OriginalReadLine
    if (-not [string]::IsNullOrWhiteSpace($line)) {
        $global:__Cosir.Active = $true
        $text = if ($line.Length -gt 2048) { $line.Substring(0, 2048) } else { $line }
        __CosirEmit 'command_started' (__CosirEscape $text)
    }
    return $line
}
__CosirEmit 'ready' "powershell;$($PSVersionTable.PSVersion)"
