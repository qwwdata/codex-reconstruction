# Diagnosis, fallback, and verification limits

[简体中文](troubleshooting.zh-CN.md) · [English overview](../README.en.md)

## Identify the failure first

`Reconnecting... 5/5` reports retries; it does not identify their cause. The evidence in this case was WebSocket connection timeouts → repeated retries → HTTP fallback, followed by a successful isolated request after enabling the existing system proxy. Browser access to ChatGPT does not establish that Codex WebSockets take the same route.

Handle these separately:

- Connection `timed out`: check the actual Codex process's proxy route, whether the proxy application is running, matching ports, and WebSocket support.
- `401` / `403` or expired login: check authentication and access policies. Changing transport does not replace authentication.
- `peer closed connection without sending TLS close_notify`: indicates early closure by the peer or an intermediary; this alone does not identify which network segment failed. Persistent occurrences need proxy/upstream and connection keepalive investigation.
- `hook exited with code 1`: check the hook command, working directory, and script exit code.
- `WinError 10013` during a proxy port probe: the execution sandbox may be blocking that socket. This does not prove the local proxy is stopped. Check the actual port in a normal user PowerShell session.

Do not disable TLS verification, paste tokens into issues, or directly edit the chat state SQLite database.

## Find the desktop app's actual codex.exe

The CLI on PATH may differ from the desktop app's version. For a Microsoft Store installation, try this read-only lookup:

```powershell
$Packages = @(Get-AppxPackage -Name 'OpenAI.Codex')
$Packages | Select-Object Name, Version, InstallLocation
```

Only if it identifies one appropriate package:

```powershell
if ($Packages.Count -eq 1) {
    $Codex = Join-Path $Packages[0].InstallLocation 'app\resources\codex.exe'
    if (Test-Path -LiteralPath $Codex) { & $Codex --version }
}
```

This layout comes from the tested desktop package and may change. If no package or executable is found, inspect the running app in Task Manager or its installation directory. Do not take ownership of WindowsApps. Once the path is confirmed, run:

```powershell
& $Codex features list
```

If `respect_system_proxy` is absent or `removed`, do not add it blindly. It was still under development in the tested build; it may be renamed, enabled by default, or removed in future versions. The script checks actual executable output for availability.

## Manually merge the system proxy setting

If the script cannot safely edit a special TOML layout such as inline `features = { ... }`, merge it manually. Fully quit the app and back up the **user-level** `config.toml`. If `[features]` exists, edit its key instead of adding a duplicate table:

```toml
[features]
# Preserve your other existing feature settings.
respect_system_proxy = true
```

For profiles or managed deployments, also check the effective configuration. The script changes only the root field in the selected file; it does not change profiles, project settings, or managed policies. Other configuration layers and command-line arguments can affect the result. Inspect existing proxy environment variables locally; the script does not remove or replace them. PAC-only setups are outside this automatic repair's scope.

## HTTP fallback (advanced manual change)

Consider this only with **an explicitly confirmed ChatGPT login, a local client currently using built-in `openai`, and a compatible version**. For API keys, company gateways, managed Work/Cloud environments, other providers, or enforced authentication policies, follow the corresponding deployment configuration instead of using this backend URL.

The URL comes from the tested local client; it is not a public API endpoint with promised long-term compatibility. Official documentation supports the custom-provider configuration structure, but does not provide a cross-version guarantee for this particular ChatGPT backend usage. The automatic script does not apply this fallback.

1. Fully quit Codex and manually back up the original user `config.toml`. That backup must also be restored manually; the script's `restore` supports only its own backups and checksum manifests.
2. Set or change the root `model_provider` at the top of the file, before any table headers. Preserve the existing model, reasoning effort, and other fields.
3. Append a separate provider table. If `openai-http` already exists, inspect it or choose an unused ID rather than overwriting it.

```toml
# Root setting: before every [table]. Edit an existing key; do not duplicate it.
model_provider = "openai-http"

# This table can be appended. Preserve all existing configuration.
[model_providers.openai-http]
name = "OpenAI via HTTP"
base_url = "https://chatgpt.com/backend-api/codex"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false
supports_standalone_web_search = true
```

See [examples/chatgpt-http.toml](../examples/chatgpt-http.toml). **Do not replace your entire config with this fragment.** `supports_standalone_web_search` only advertises a compatible endpoint capability; it does not enable search by itself. Official documentation still describes standalone search as under development. This configuration has not received full search-function testing.

Reopen the app and **create a new chat** to verify HTTP transport and a completed reply. In this case, existing chats retained their original `openai` provider; changing the default and restarting did not migrate them. Repairing their WebSocket proxy route is therefore the first option for existing chats. Do not force migration by editing the database.

To roll back, fully quit the app and restore the manual backup. Merge manually if there have been other edits. Chats created with the new provider may retain it, so keep its definition if those chats still require it.

Do not create `[model_providers.openai]`: `openai`, `ollama`, and `lmstudio` are [reserved IDs in the official documentation](https://learn.chatgpt.com/docs/config-file/config-advanced). Provider settings belong in user configuration; do not rely on project `.codex/config.toml` to override them. See the [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) for field definitions.

## Observed verification

These are a few comparative requests on one Windows machine, using the same model and reasoning settings, not a broad performance benchmark. Only summary statistics are published, without user configuration, chat content, or authentication data:

- Original route: WebSocket prewarming and connection timeouts, retries 1/5 through 5/5, HTTP fallback after approximately 100 seconds.
- Built-in provider with explicit proxy variables set only in the test child process: completed reply in about 11.02 seconds, one WebSocket attempt, zero retries, zero HTTP fallbacks.
- Built-in provider with `respect_system_proxy`, without injecting extra proxy variables into the test: completed reply in about 11.66 seconds, one WebSocket attempt, zero retries, zero HTTP fallbacks.
- Separate HTTP provider: completed reply in about 16 seconds, zero WebSocket attempts, zero retries.
- Later real existing chat: one TLS disconnect retry, then successful WebSocket establishment and reuse by a subsequent request. No five-retry sequence was observed on that turn.

Our inference is that routing this client through its existing system proxy resolved the persistent connection timeouts in this case. We did not prove the full underlying cause through packet capture, or establish that every network failure disappeared.

Five attempts reflect a retry budget. The [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) lists `stream_max_retries` as defaulting to five and describes it as retries for SSE stream interruptions. That field's documentation is not a complete implementation explanation of this case's WebSocket retry path. The tool does not change it.

## Log summary limitations

The default database is `$CODEX_HOME/logs_2.sqlite` or `$HOME/.codex/logs_2.sqlite`. The script copies the main file and any existing WAL to a local temporary directory, reads the copy, then deletes its own temporary directory. It does not use `immutable=1`, which ignores the WAL, or write to the original database.

Copying a live database is not a transactional snapshot and may be inconsistent; fully quit Codex and retry on errors. Counts represent events: multiple log entries may describe one failure, and `timeout_events` can include other timeout types. Without `--thread-id`, global counts do not establish a single chat's outcome. Future logging changes may prevent counting or produce underestimates.

If continuous retries remain, file an issue with the version and anonymized counts. State whether the app was fully reopened, whether the system proxy is running, and whether you verified the original chat. Do not upload your `.codex` directory, full logs, or backups.

