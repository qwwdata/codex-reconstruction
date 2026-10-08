# Codex reconnect repair

**English** · [简体中文](README.md)

Diagnosis and repair for Windows Codex repeatedly showing `Reconnecting... 1/5` through `5/5`, followed by a long delay before replying. This repository comes from a real repair: enabling an available Codex feature so it uses an already configured Windows system proxy and can establish its WebSocket connection.

The intended scope is **local Windows Codex, an enabled static system proxy, and WebSocket connection timeouts in the logs**. Direct networks, PAC-only proxies, authentication failures, service incidents, and organization gateways require separate diagnosis. This cannot guarantee the elimination of every disconnect.

## Why five reconnect attempts?

In this case, WebSocket connections repeatedly timed out. Only after exhausting retries did the logs show `falling back to HTTP`; the initial request took roughly 100 seconds to get started. After enabling system proxy support, a separate test using the same built-in provider completed a reply in about 11.66 seconds, with one WebSocket connection attempt and zero retries.

Tested with **`codex-cli 0.162.0-alpha.2`**, desktop package **`26.1002.6548.0`**, on **2026-10-07 through 2026-10-08**. That executable listed `respect_system_proxy` as `under development` and disabled by default. A later real chat still had one `TLS close_notify` disconnect and recovered on retry. The evidence therefore supports an improvement to repeated connection timeouts.

These are local observations and controlled comparisons, not proof that every five-retry failure is caused by a proxy. See [diagnosis, HTTP fallback, and evidence](docs/troubleshooting.en.md).

## Quick start (PowerShell)

Download and extract the repository ZIP, or clone it. Open **Windows PowerShell** in the repository root. You need **Python 3.11 or later**; no third-party dependencies or administrator privileges are required.

Confirm the actual Python and Codex executables:

```powershell
$Python = (Get-Command python -ErrorAction Stop).Source
& $Python -X utf8 --version
$Codex = (Get-Command codex -ErrorAction Stop).Source
& $Codex --version
```

If `python` resolves to a broken WindowsApps placeholder, set `$Python` to the absolute path of an installed `python.exe`. If Codex is absent from PATH, or the CLI version differs from the desktop version, set `$Codex` to the **actual executable used by the desktop app**. See [how to locate it](docs/troubleshooting.en.md#find-the-desktop-apps-actual-codexexe).

### 1. Read-only diagnosis

```powershell
& $Python -X utf8 .\scripts\codex_reconnect.py diagnose --codex $Codex
```

Check:

- `respect_system_proxy.available`: the feature must exist and must not be `removed` or `not listed`.
- `windows_proxy.enabled` and `windows_proxy.static_proxy`: both must be `true`.
- `config_flag_enabled`: whether the user configuration already enables the feature.
- `proxy_environment_variables_present`: existing proxy variable names; inspect their values locally.

This sends no model request. The default config is `$env:CODEX_HOME\config.toml`, or `$HOME\.codex\config.toml` when the environment variable is unset. Add `--config 'actual config path'` for a different location. An existing config file is required.

### 2. Preview and apply

**Fully quit Codex, including its background process**, to avoid concurrent configuration edits. Keep your system proxy application running.

```powershell
& $Python -X utf8 .\scripts\codex_reconnect.py apply-system-proxy --codex $Codex --dry-run
& $Python -X utf8 .\scripts\codex_reconnect.py apply-system-proxy --codex $Codex
```

If using `--config`, pass the same path to both commands. The second command writes the change after a successful preview; there is no interactive confirmation. The only setting changed is:

```toml
[features]
respect_system_proxy = true
```

The script merges the existing `[features]` table, preserves other fields, comments, a UTF-8 BOM and existing line endings, reparses the candidate TOML, and verifies that only this field changed. Layouts it cannot safely edit are rejected with instructions to edit manually. Running it again when the feature is already enabled creates no extra backup.

The original config is backed up under `reconnect-backups/` alongside the config. The output's `backup` value is the rollback path. **Backups may contain secrets; keep them local.**

### 3. Reopen and verify the affected chat

Reopen Codex and send a short request in the chat that previously failed. Confirm a complete reply. Optionally summarize the last ten minutes of logs:

```powershell
& $Python -X utf8 .\scripts\codex_reconnect.py check-logs --minutes 10
```

By default, this includes every chat and process in that database. Add `--thread-id 'actual chat ID'` to inspect one chat; use `--logs 'actual database path'` for another log location. Output contains event counts, without prompts, tokens, raw log lines, or the chat ID.

`stream_retry_events` and `http_fallbacks` help comparison, but zero counts alone do not prove success; missing debug logging can also produce zero counts. The live snapshot is best effort. `truncated: true` means only the latest 10,000 records were counted. Combine counts with an actual completed reply and the affected chat's logs.

### 4. Roll back

Fully quit Codex, then use the `backup` path printed by the repair:

```powershell
$Backup = 'replace with the printed absolute .toml.bak path'
& $Python -X utf8 .\scripts\codex_reconnect.py restore --backup $Backup --dry-run
& $Python -X utf8 .\scripts\codex_reconnect.py restore --backup $Backup
```

If the config has changed since the repair, restore refuses to overwrite it; merge the backup manually. Reopen Codex after restoring. A custom config still requires the same `--config` path.

## HTTP fallback and common pitfalls

If system proxy support does not apply, see the [HTTP fallback](docs/troubleshooting.en.md#http-fallback-advanced-manual-change). It requires **an explicitly confirmed ChatGPT login, a local client, and a compatible version**. Do not apply it to API-key authentication, organization gateways, or another provider.

- Give a custom provider its own ID; do not override built-in `openai`.
- A default provider change **does not automatically migrate existing chats**. They may retain their original provider.
- The tested version lists `responses_websockets` / `responses_websockets_v2` as `removed`; old advice to disable these flags does not apply.
- Reducing the retry count shortens waiting; it does not repair connectivity.
- `hook exited with code 1` is a hook script failure and needs separate diagnosis.

The tool does not change the Windows registry, persist environment variables, change the model or reasoning effort, read `auth.json`, or disable TLS certificate checks. Diagnosis is local; applying the repair sends no model request.

## Tests and feedback

```powershell
& $Python -X utf8 -m unittest discover -s tests -v
```

Tests use temporary configs and synthetic logs without changing your real Codex config. CI runs the same suite on Windows and Linux with Python 3.11–3.13. Local Windows / Python 3.12 tests pass; consult GitHub Actions for actual CI results.

When filing an issue, include your OS, Codex version, anonymized diagnostics, whether the affected chat completed its reply, and retry counts. Remove local paths from output first. **Do not attach config backups, authentication files, or full log databases.**

## References and license

Official documentation covers [configuration fields](https://learn.chatgpt.com/docs/config-file/config-reference) and [custom providers and reserved IDs](https://learn.chatgpt.com/docs/config-file/config-advanced). `respect_system_proxy` was absent from the checked configuration reference. Its availability, stage and behavior come from the tested executable and local experiments; the script checks feature availability before editing.

This is a community troubleshooting project, unaffiliated with OpenAI. Code and documentation are available under the [MIT License](LICENSE).

