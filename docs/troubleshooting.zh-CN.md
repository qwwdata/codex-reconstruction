# 诊断、备用方案与验证边界

[English](troubleshooting.en.md) · [返回中文入口](../README.md)

## 先确认是哪一种问题

`Reconnecting... 5/5` 是重试表现，不能单独确定原因。本案例的证据链是 WebSocket 建连超时 → 多次重试 → HTTP 回退，启用现有系统代理后独立请求成功。浏览器能访问 ChatGPT 并不能证明 Codex 的 WebSocket 使用同一路径。

以下问题应分别处理：

- 建连 `timed out`：检查实际 Codex 进程的代理路线、代理软件是否运行、端口是否匹配、代理是否允许 WebSocket。
- `401` / `403`、登录过期：检查认证与访问策略；切换传输不能代替重新认证。
- `peer closed connection without sending TLS close_notify`：对端或中间设备提前关闭连接的表现，不能仅凭此确定是哪一段线路。若仍频繁出现，检查代理软件/上游线路和连接保活。
- `hook exited with code 1`：查钩子命令、工作目录与脚本退出码。
- 代理端口探测出现 `WinError 10013`：可能是当前执行沙箱不允许该连接，不能据此断言本机代理未启动。应在普通用户 PowerShell 中核查实际端口。

不要为此关闭 TLS 验证、把令牌贴到 issue，或直接修改聊天状态 SQLite 数据库。

## 找到桌面版实际使用的 codex.exe

PATH 中的 Codex CLI 可能和桌面程序使用的版本不同。Microsoft Store 安装可尝试只读查询：

```powershell
$Packages = @(Get-AppxPackage -Name 'OpenAI.Codex')
$Packages | Select-Object Name, Version, InstallLocation
```

仅当找到一个正确的安装包时：

```powershell
if ($Packages.Count -eq 1) {
    $Codex = Join-Path $Packages[0].InstallLocation 'app\resources\codex.exe'
    if (Test-Path -LiteralPath $Codex) { & $Codex --version }
}
```

该目录结构来自已测试的桌面包，未来可能不同。无结果或路径不存在时，在任务管理器/安装目录核对正在使用的程序；不要强行取得 WindowsApps 目录的所有权。确认路径后运行：

```powershell
& $Codex features list
```

若没有 `respect_system_proxy`，或阶段为 `removed`，不要硬加这个开关。它在所测版本仍处于开发中，未来可能改名、变为默认启用或移除；脚本只根据实际输出判断是否可用。

## 手动合并系统代理开关

自动脚本不能安全编辑内联 `features = { ... }` 等特殊 TOML 布局时，需要手动合并。完全退出应用，备份**用户级** `config.toml`。已有 `[features]` 就修改其中的键，不要复制第二个同名表：

```toml
[features]
# 保留已有的其他开关
respect_system_proxy = true
```

配置使用 profile 或受组织管理时，还应核对实际生效的配置。该脚本仅修改所指定文件的根级字段，不改 profile、项目配置或管理策略；其他配置层和命令行参数可能影响实际结果。已有代理环境变量也需核对，脚本不会删掉或替换它们。PAC-only 设置不属于本次自动修复的范围。

## HTTP 备用方案（高级手动操作）

仅在**本地客户端明确使用 ChatGPT 登录、当前是内置 `openai` provider、且版本兼容**时考虑此方案。如果正在使用 API key、公司网关、Work/Cloud 的托管环境、其他 provider 或强制认证策略，请先遵循相应部署配置，不要套用这里的后端地址。

这个地址来自所测本地客户端，不是承诺长期兼容的公开 API endpoint。官方文档支持自定义 provider 的配置结构，但没有为此具体 ChatGPT 后端用法提供跨版本保证。自动脚本不会应用此方案。

1. 完全退出 Codex，手动备份原用户 `config.toml`。该备份的恢复也需要手动操作；脚本的 `restore` 仅支持它自己创建的备份与校验清单。
2. 在文件最上方、所有表标题之前设置或修改根级 `model_provider`。保留原有模型、推理等级等字段。
3. 在文件末尾加入独立 provider 表。若已有 `openai-http`，不要覆盖，先核对内容或选择未使用的 ID。

```toml
# 根级设置：必须在所有 [table] 之前；已有此键时修改，不要重复。
model_provider = "openai-http"

# 下面的表可以加到文件末尾。请保留所有原有配置。
[model_providers.openai-http]
name = "OpenAI via HTTP"
base_url = "https://chatgpt.com/backend-api/codex"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false
supports_standalone_web_search = true
```

片段见 [examples/chatgpt-http.toml](../examples/chatgpt-http.toml)，**不要拿它替换完整配置**。`supports_standalone_web_search` 只是保留该兼容端点的能力声明，不会单独启用搜索；独立搜索在官方文档中仍处于开发中。上述配置没有经过搜索功能的完整验证。

重新打开应用，**新建聊天**验证 HTTP 传输及完整回复。旧聊天在本案例中继续使用原有 `openai` provider；改默认值和重启都没有自动迁移它。因此旧聊天更适合先修复其 WebSocket 的代理路线。不要通过改数据库强行迁移。

回滚时完全退出应用，恢复手动备份；若已经有其他新改动，手动合并。使用新 provider 创建的聊天也可能保留该 provider，尚需继续使用时不要删除其定义。

不要创建 `[model_providers.openai]`：`openai`、`ollama`、`lmstudio` 是[官方文档规定的保留 ID](https://learn.chatgpt.com/docs/config-file/config-advanced)。provider 相关设置应位于用户配置，不能依赖项目级 `.codex/config.toml` 覆盖。配置字段解释见[官方配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)。

## 实际验证记录

这是同一台 Windows 机器、同一模型与推理设置的少量对照请求，不能当作广泛性能基准。公开内容仅记录统计，不包含真实用户配置、聊天文本或认证数据：

- 原始路径：WebSocket 预热与建连超时，连续重试 1/5 至 5/5，约 100 秒后 HTTP 回退。
- 内置 provider + 仅测试子进程的显式代理变量：完整回复成功，约 11.02 秒，WebSocket 建连 1 次、重试 0 次、HTTP 回退 0 次。
- 内置 provider + `respect_system_proxy`、没有为测试额外注入代理变量：完整回复成功，约 11.66 秒，WebSocket 建连 1 次、重试 0 次、HTTP 回退 0 次。
- 独立 HTTP provider：完整回复成功，约 16 秒，WebSocket 建连 0 次、重试 0 次。
- 后续真实旧聊天：观察到一次 TLS 断线重试，随后 WebSocket 成功并被后续请求复用；该次未再出现连续五次重连。

我们据此推断：让该客户端使用已有系统代理，修复了本例的持续建连超时。没有用抓包证明完整底层原因，也没有证明所有网络问题都已消失。

五次只是重试预算的表现。[配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)将 `stream_max_retries` 的默认值列为 5，并描述为 SSE 流中断的重试次数；不能把文档这一项直接当作本例 WebSocket 重试路径的完整实现说明。工具不会修改该值。

## 日志统计限制

默认日志库是 `$CODEX_HOME/logs_2.sqlite` 或 `$HOME/.codex/logs_2.sqlite`。脚本复制主文件与存在的 WAL 到本地临时目录，再读取复制件，结束后删除自己的临时目录。不使用忽略 WAL 的 `immutable=1`，也不写原日志库。

活跃写入时复制不是事务级快照，可能不一致；遇到错误应退出 Codex 后重试。输出按事件统计，同一错误可能被多条日志重复记录，`timeout_events` 也可能包含其他类型超时。无 `--thread-id` 时不能把全局计数当作单个聊天的结果。未来日志格式变化可能导致无法统计或低估。

仍有连续重试时，带版本与匿名统计提交 issue，并说明是否已经完全重开应用、系统代理是否运行、是否在原聊天中验证。不要上传 `.codex` 目录、完整日志或备份。

