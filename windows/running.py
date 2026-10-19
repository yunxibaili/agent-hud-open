"""Observe live agent processes; never infer liveness from saved transcripts."""

import os

import winx


_SCAN = r"""
$ErrorActionPreference = 'Stop'
$session = (Get-Process -Id __HUD_PID__).SessionId
$processes = @(Get-CimInstance Win32_Process -Filter "SessionId=$session")
$excluded = [System.Collections.Generic.HashSet[uint32]]::new()
[void]$excluded.Add([uint32]__HUD_PID__)
do {
    $previous = $excluded.Count
    foreach ($entry in $processes) {
        if ($excluded.Contains([uint32]$entry.ParentProcessId)) {
            [void]$excluded.Add([uint32]$entry.ProcessId)
        }
    }
} while ($previous -ne $excluded.Count)
$native = @{
    'codex.exe'='codex'; 'codex-cli.exe'='codex'; 'codex-tui.exe'='codex'
    'opencode.exe'='opencode'; 'opencode-desktop.exe'='opencode'
    'antigravity.exe'='antigravity'; 'agy.exe'='antigravity'
    'workbuddy.exe'='workbuddy'; 'codebuddy.exe'='codebuddy'
    'dsh.exe'='dsh'; 'deepseek-harness.exe'='dsh'; 'deepseek harness.exe'='dsh'; 'dsh-desktop.exe'='dsh'
    'claude.exe'='claude'; 'kimi.exe'='kimi'; 'qwen.exe'='qwen'; 'qwen-code.exe'='qwen'
    'openclaw.exe'='openclaw'; 'clawdbot.exe'='openclaw'; 'devin.exe'='devin'
    'hermes.exe'='hermes'; 'zcode.exe'='zcode'; 'copilot.exe'='copilot'; 'grok.exe'='grok'
}
$packages = @{
    'codex'='[\/]node_modules[\/]@openai[\/]codex[\/]'
    'opencode'='[\/]node_modules[\/](?:opencode-ai|@opencode-ai[\/][^\/]+)[\/]'
    'claude'='[\/]node_modules[\/]@anthropic-ai[\/]claude-code[\/]'
    'codebuddy'='[\/]node_modules[\/]@tencent-ai[\/]codebuddy-code[\/]'
    'pi'='[\/]node_modules[\/]@(?:mariozechner|earendil-works)[\/]pi-coding-agent[\/]'
    'qwen'='[\/]node_modules[\/]@qwen-code[\/]qwen-code[\/]'
    'openclaw'='[\/]node_modules[\/](?:openclaw|clawdbot)[\/]'
    'dsh'='[\/](?:deepseek-harness|@deepseek-ai[\/]harness|ai\.deepseek\.harness\.desktop)[\/]'
    'kimi'='(?:[\/]kimi(?:-cli|-code)?[\/].*\.py(?:\s|"|$)|\s-m\s+kimi_cli(?:\s|\.|$))'
    'hermes'='(?:[\/]hermes_cli[\/].*\.py(?:\s|"|$)|\s-m\s+hermes_cli(?:\s|\.|$))'
    'devin'='[\/]node_modules[\/]@cognition-ai[\/]devin[\/]'
    'grok'='[\/]node_modules[\/]@xai[\/]grok[\/]'
}
$found = [System.Collections.Generic.HashSet[string]]::new()
foreach ($entry in $processes) {
    if ($excluded.Contains([uint32]$entry.ProcessId)) { continue }
    $name = $entry.Name.ToLowerInvariant()
    if ($native.ContainsKey($name)) { [void]$found.Add($native[$name]); continue }
    if ($name -notmatch '^(?:node|node64|bun|deno|python(?:w|[\d.]+)?)\.exe$') { continue }
    $arguments = @([regex]::Matches($entry.CommandLine, '"([^"]*)"|(\S+)') | ForEach-Object {
        if ($_.Groups[1].Success) { $_.Groups[1].Value } else { $_.Groups[2].Value }
    })
    $entrypoint = ''
    for ($index = 1; $index -lt $arguments.Count; $index++) {
        $argument = $arguments[$index]
        if ($argument -in @('-e', '--eval', '-p', '--print', '-c')) { break }
        if ($argument -eq '-m' -and $index + 1 -lt $arguments.Count) {
            $entrypoint = ' -m ' + $arguments[$index + 1]
            break
        }
        if ($argument -match '\.(?:[cm]?js|py)$') { $entrypoint = $argument; break }
    }
    foreach ($agent in $packages.Keys) {
        if ($entrypoint -match $packages[$agent]) { [void]$found.Add($agent) }
    }
}
'HUD_PROCESS_SCAN_OK'
$found | Sort-Object
"""


def snapshot():
    # Only agent ids cross the subprocess boundary; command lines are never logged.
    output = winx.powershell(_SCAN.replace("__HUD_PID__", str(os.getpid())), timeout=8)
    lines = output.splitlines()
    if "HUD_PROCESS_SCAN_OK" not in lines:
        return None
    return set(lines) - {"HUD_PROCESS_SCAN_OK", ""}


def watch(out, stop):
    while not stop.is_set():
        try:
            agents = snapshot()
        except Exception:
            agents = None
        if stop.is_set():
            return
        out.put(agents)
        stop.wait(10)
