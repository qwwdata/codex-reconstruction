# Codex 重连修复 / Codex reconnect repair

[English](README.en.md) · **简体中文**

针对 Windows 上 Codex 反复显示 `Reconnecting... 1/5` 到 `5/5`、等待很久才开始回复的排查与修复工具。来自一次真实修复：让支持该功能的 Codex 版本使用已经配置好的 Windows 系统代理，恢复 WebSocket 连接。

适用范围是**本地 Windows Codex、已启用静态系统代理、连接日志出现 WebSocket 超时**。其他网络、PAC 自动代理、账号认证错误、服务故障和组织网关需要分别排查。这个方案不能保证消除所有断线。

## 为什么会重连五次？

本案例日志显示 WebSocket 建连反复超时，重试用尽后才出现 `falling back to HTTP`，第一次请求约等待 100 秒。启用系统代理后，同一内置 provider 的独立测试完成回复，用时约 11.66 秒，WebSocket 建连 1 次、重试 0 次。

测试版本为 **`codex-cli 0.162.0-alpha.2`**，桌面包版本为 **`26.1002.6548.0`**，验证日期 **2026-10-07 至 2026-10-08**。该版本的 `respect_system_proxy` 标为 `under development`，默认关闭。后续真实聊天仍发生过一次 `TLS close_notify` 断线并重试成功，所以这里记录的是连续超时重连问题的改善。

以上是本地观察与对照测试，不代表所有“重连 5 次”都由代理导致。详情见[诊断、HTTP 备用方案与证据说明](docs/troubleshooting.zh-CN.md)。

## 快速开始（PowerShell）

下载仓库 ZIP 并解压，或克隆仓库，然后在仓库根目录打开 **Windows PowerShell**。需要 **Python 3.11 或更高版本**，不需要安装第三方依赖，也不需要管理员权限。

先确认 Python 和 Codex 的实际路径：

```powershell
$Python = (Get-Command python -ErrorAction Stop).Source
& $Python -X utf8 --version
$Codex = (Get-Command codex -ErrorAction Stop).Source
& $Codex --version
```

如果 `python` 指向无效的 WindowsApps 占位程序，将 `$Python` 改为你已安装的 `python.exe` 的绝对路径。若 Codex 不在 PATH，或 CLI 与桌面版版本不同，将 `$Codex` 指向**桌面应用实际使用的 `codex.exe`**，定位方法见[详细说明](docs/troubleshooting.zh-CN.md#找到桌面版实际使用的-codexexe)。

### 1. 只读诊断

```powershell
& $Python -X utf8 .\scripts\codex_reconnect.py diagnose --codex $Codex
```

检查结果中的：

- `respect_system_proxy.available`：是否支持，不能是 `removed` 或 `not listed`。
- `windows_proxy.enabled` 和 `windows_proxy.static_proxy`：是否都为 `true`。
- `config_flag_enabled`：用户配置是否已经启用该开关。
- `proxy_environment_variables_present`：是否有代理环境变量；脚本仅显示变量名，值请在本地核对。

这一步不发送模型请求。默认读取 `$env:CODEX_HOME\config.toml`，未设置时读取 `$HOME\.codex\config.toml`；不同位置可加 `--config '实际配置路径'`。已有配置文件必须存在。

### 2. 预览后修复

先**完全退出 Codex，包括后台进程**，避免配置被同时改写。保持系统代理软件正常运行。

```powershell
& $Python -X utf8 .\scripts\codex_reconnect.py apply-system-proxy --codex $Codex --dry-run
& $Python -X utf8 .\scripts\codex_reconnect.py apply-system-proxy --codex $Codex
```

使用自定义 `--config` 时，两条命令都要传同一个路径。预览成功后，第二条命令才写入；程序没有交互式确认。修复仅设置：

```toml
[features]
respect_system_proxy = true
```

脚本合并现有 `[features]`，保留其他字段、注释、UTF-8 BOM 和原有行尾，重新解析 TOML 并确认只有该字段发生变化。不支持安全修改的布局会拒绝写入，并提示手动修改。已经启用时重复执行不产生新备份。

原配置备份保存到配置目录下的 `reconnect-backups/`，输出中的 `backup` 是回滚路径。**备份可能含密钥，应留在本机。**

### 3. 重开应用并验证原聊天

重新打开 Codex，在之前出问题的聊天发送一个简短请求，确认能收到完整回复。可以只统计最近 10 分钟的日志：

```powershell
& $Python -X utf8 .\scripts\codex_reconnect.py check-logs --minutes 10
```

默认统计该数据库中的全部聊天和进程。要验证具体聊天，追加 `--thread-id '实际聊天ID'`；日志位置不同可用 `--logs '实际日志数据库路径'`。输出只包含事件计数，不输出提示词、令牌、原始日志或聊天 ID。

`stream_retry_events` 和 `http_fallbacks` 可帮助比较，但计数为零不能单独证明连接成功；缺少调试日志也会产生零计数。日志快照是尽力读取，`truncated: true` 表示只统计了最近 10,000 条记录。请结合实际完成的回复和该聊天的日志判断。

### 4. 回滚

完全退出 Codex，将修复输出的 `backup` 路径填入：

```powershell
$Backup = '替换为修复输出的 .toml.bak 绝对路径'
& $Python -X utf8 .\scripts\codex_reconnect.py restore --backup $Backup --dry-run
& $Python -X utf8 .\scripts\codex_reconnect.py restore --backup $Backup
```

如果修复后又改过配置，脚本会拒绝覆盖，需要手动对照备份合并。回滚完成后重新打开 Codex。自定义配置路径仍须传入相同的 `--config`。

## HTTP 备用方案与常见陷阱

如系统代理方案不适用，可阅读[HTTP 备用方案](docs/troubleshooting.zh-CN.md#http-备用方案高级手动操作)。该方案需要**明确使用 ChatGPT 登录、本地客户端和兼容版本**；不要套用于 API key、组织网关或其他 provider。

- 自定义 provider 使用独立 ID，不能覆盖内置 `openai`。
- 更改默认 provider **不自动迁移旧聊天**；旧聊天可能继续使用创建时的 provider。
- 本次版本中的 `responses_websockets` / `responses_websockets_v2` 已标为 `removed`，旧教程里的关闭开关方法不适用。
- 降低重试次数只是减少等待，不会修复连接。
- `hook exited with code 1` 属于钩子脚本失败，需要另行排查。

工具不修改 Windows 注册表、不持久修改环境变量、不改模型或推理等级、不读 `auth.json`，也不关闭 TLS 证书校验。脚本诊断是本地检查，修复不会发起模型请求。

## 测试与反馈

```powershell
& $Python -X utf8 -m unittest discover -s tests -v
```

测试使用临时配置与合成日志，不操作真实 Codex 配置；CI 在 Windows 和 Linux、Python 3.11–3.13 上运行同一测试。当前本地 Windows / Python 3.12 测试通过，CI 状态以 GitHub Actions 实际结果为准。

提交 issue 时可提供操作系统、Codex 版本、匿名化诊断结果、原聊天是否完成回复及重试次数。请先删除输出中的本机路径，**不要提交配置备份、认证文件或完整日志数据库**。

## 参考与授权

官方文档解释[配置字段](https://learn.chatgpt.com/docs/config-file/config-reference)和[自定义 provider 与保留 ID](https://learn.chatgpt.com/docs/config-file/config-advanced)。`respect_system_proxy` 未出现在核对的配置参考页面中；其存在、阶段和效果来自所测可执行文件与本地实验，脚本会在修改前检查当前版本是否支持。

本仓库为社区排查方案，与 OpenAI 官方项目无隶属关系。代码和文档采用 [MIT License](LICENSE)。

