# frp 常驻与 ShellDeck 按需启动

VPS 的 frps 和本机的 frpc 独立常驻。ShellDeck 用 `shelldeck` 在当前终端运行，在管理菜单输入 `quit` 后退出，不自动重启。Linux 和 macOS 使用同一套安装与启动命令，不需要 tmux 或 ShellDeck 系统服务。

## 安装前提与顺序

1. VPS 安装 frps、Python 3 和 nftables，准备 token、TLS 证书、[frps.toml](../examples/frp/frps.toml) 和 [access.json](../examples/frp/access.json)。先应用白名单，再运行 frps。
2. 本机准备同版本 frpc。已有常驻服务直接复用；按原服务方式加载 ShellDeck TCP 代理。示例公网端口为 **9099**，本地目标为 **127.0.0.1:19000**，控制端口为 7000，frp 按 0.71.1 验证。
3. 本机准备 Python 3.11+；Python 3.9/3.10 使用 TOML 时先安装 `requirements-frp.txt`。frpc 与 ShellDeck 必须位于同一网络命名空间。
4. 准备仅引用现有 frpc 配置的 ShellDeck JSON，运行一次 `python3 scripts/install.py`，以后只运行 `shelldeck`。

TLS 签发、可信证书复制和原生 frp 参数见 [frp 使用说明](frp.md)。ShellDeck 不下载或安装 frp。实际地址、凭据、配置、备份、测试报告和日志放在仓库外。

## frp 的独立常驻配置

已有 frpc 系统服务时继续使用它，不另建第二个实例。首次安装可参考：

- Linux：[frpc.service](../examples/frp/frpc.service)，放到 `~/.config/systemd/user/frpc.service`，填写程序与配置绝对路径，再运行 `systemctl --user daemon-reload`、`systemctl --user enable --now frpc.service`。用户服务需要注销后继续运行时，由管理员执行一次 `sudo loginctl enable-linger "$USER"`。
- macOS：[frpc.plist](../examples/frp/frpc.plist)，填写程序、配置和日志绝对路径，创建日志目录后放到 `~/Library/LaunchAgents/com.shelldeck.frpc.plist`，执行 `launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.shelldeck.frpc.plist"`。该用户服务在登录期间运行，休眠会影响连接。

以上启动项只属于 frpc。若 frpc TOML 使用 `{{ .Envs.… }}`，变量仅提供给 frpc 自己的环境；systemd 可用私有 `EnvironmentFile`，launchd 可用 `EnvironmentVariables`。先以原服务环境运行 `frpc verify -c /absolute/path/to/frpc.toml`，启动后核对代理成功日志，再做真实回连。

## ShellDeck 配置与命令

[shelldeck.json](../examples/frp/shelldeck.json) 只需三个字段：

```json
{
  "frpc_config": "~/.config/shelldeck-frp/frpc.toml",
  "proxy_name": "shelldeck",
  "agent_socket": "~/.config/shelldeck/agent/control.sock"
}
```

新部署保存为 `~/.config/shelldeck/config.json`，把 `frpc_config` 改为现有代理定义的位置。已有 `~/.config/shelldeck-frp/service.json` 可直接复用。两者只需保留一份；安装器优先选前者，不存在才选旧路径。绝对 `XDG_CONFIG_HOME` 可替换 `~/.config`。显式 `--config` 不存在或错误时直接失败，不回退其他文件。

`frpc_config` 支持 TOML 或 JSON，读取 `proxy_name` 精确匹配的唯一代理。所选代理必须显式使用 `type=tcp`、IPv4 回环 `localIP`、有效整数 `localPort` 和 `transport.proxyProtocolVersion=v1`；被非空 `start` 排除、插件代理、名称重复或公开监听地址都会拒绝。它只读取连接约定，不验证 frpc 是否在线，不展开凭据，也不会修改原文件。

代理名称、地址、端口和协议必须为静态值。其他字段内带引号的环境模板可保留；任意 Go 模板循环/条件、YAML/INI 不支持。使用 `includes` 时，直接指向定义该代理的静态片段。文件路径支持 `~`，相对路径按 ShellDeck JSON 所在目录解析。未设置 `agent_socket` 时使用配置根目录下的 `shelldeck/agent/control.sock`。

在仓库目录执行一次：

```sh
python3 scripts/install.py
# 自定义配置时：
python3 scripts/install.py --config /absolute/path/to/existing.json
```

生成的 `~/.local/bin/shelldeck` 直接执行本项目的 `shelldeck.py --config ...`，使用安装时的 Python。需要定制命令位置时使用 `--command-path`。安装不会启动程序、创建服务或调用 SSH；不会覆盖其他程序的同名命令。确认 `~/.local/bin` 在 `PATH` 中；缺少时在自己的 shell 配置文件中加入：

```sh
export PATH="$HOME/.local/bin:$PATH"
```

之后每次使用：

```sh
shelldeck
# 长任务可设置超时，0 表示关闭空闲超时：
shelldeck -t 1800
```

这是前台交互程序，需要当前终端。`ls` 列出会话，`i ID` 进入会话，Ctrl+] 返回菜单，在菜单输入 `quit` 停止。退出会释放监听与控制 socket，并断开客户端；frpc/frps 继续运行。ShellDeck 没运行时，公网端口仍可监听，但不能建立可用会话。再次启动后客户端重新回连；原会话不恢复。CLI/MCP 只在 ShellDeck 运行期间连接同一个 socket。

每次启动重新读取配置，无需重装命令。与配置冲突的 `-H` / `-p` 会被拒绝；修改本地目标应改原 frpc 配置，让 frpc 加载变更，再重启 ShellDeck。移动仓库或更换 Python 后重新运行安装器。原有 `python3 shelldeck.py -H ... -p ...` 直连用法继续可用。

## VPS 白名单

`access.json` 保留 `local_source`、`callback_sources`、`control_port` 和 `callback_port`：本机出口可访问控制和回连端口，其他回连来源只允许访问回连端口。示例分别为 7000 和 9099。它们需与 frps 的 `bindPort` / `allowPorts`、frpc 的 `serverPort` / `remotePort` 对应。

在 VPS 使用 root 执行，路径按实际安装位置填写：

```sh
SHELLDECK_ALLOWLIST_SCRIPT=/usr/local/libexec/shelldeck-frp/allowlist.py
SHELLDECK_ACCESS_CONFIG=/etc/shelldeck-frp/access.json
python3 "$SHELLDECK_ALLOWLIST_SCRIPT" --config "$SHELLDECK_ACCESS_CONFIG" apply
python3 "$SHELLDECK_ALLOWLIST_SCRIPT" --config "$SHELLDECK_ACCESS_CONFIG" status
```

配置归 root 所有、权限 0600，目录不可被普通用户写入。白名单单元采用 `Type=oneshot`、`RemainAfterExit=yes`，`ExecStart` / `ExecReload` 执行 apply；frps 以非登录服务账户运行，通过 `Requires` / `After` 依赖白名单单元。修改系统单元后验证并执行 `systemctl daemon-reload`。

脚本只原子替换专用 `inet shelldeck_frp` 表，链优先级为 -10，仅过滤两个配置端口；不要执行 `nft flush ruleset`。修改端口前先停止对应监听，同步修改两端 frp 和 access 配置，先应用白名单，再恢复监听。

本机公网出口变化后，通过已认证 SSH 执行远端脚本的 `refresh-local`。使用已经配置好的 SSH 别名和实际远端路径，例如：

```sh
ssh YOUR_SSH_HOST python3 /usr/local/libexec/shelldeck-frp/allowlist.py --config /etc/shelldeck-frp/access.json refresh-local
```

该 SSH 目标需要能免交互执行命令并具有 root 权限；使用 sudo 时需配置受限命令并保留可信的 `SSH_CONNECTION`。脚本从 SSH 会话读取本机出口，保留额外回连来源和端口，写配置失败时恢复原规则。新受管服务器需先加入 `callback_sources` 再 apply。日常运行 ShellDeck 不做这些远端管理操作；本机主动连接 VPS，通常不需要 DDNS。

## 从旧后台管理迁移

迁移会断开旧 ShellDeck 会话，安排客户端稍后重新连接。先把原 `service.json`、启动项和管理命令备份到仓库外。

1. 停止并移除 **ShellDeck** 的旧启动项，防止下次登录重新运行：macOS 用 `launchctl bootout "gui/$(id -u)/旧服务名称"`，再把对应 LaunchAgent 移到备份目录；Linux 用 `systemctl --user disable --now 旧服务名称.service`，移走对应 unit，再运行 `systemctl --user daemon-reload`。名称和路径以原配置为准，不操作独立 frpc 启动项。旧监督进程正常退出时会清理它创建的 tmux 会话。
2. 若旧版是 frpc 与 ShellDeck 合并托管，停止旧项也会停止 frpc。此时将原 frpc 配置交给前面的独立服务，确认代理正常后继续；不要同时运行两份 frpc。
3. 原 JSON 保留 `frpc_config`、`proxy_name`、`agent_socket`。删除旧 `project_root`、`python_binary`、`tmux_*`、`service_*`、`launch_agent_*`、`command_path`、`log_directory`、`environment` 和 SSH 管理字段；新入口只读取三个连接字段，其余旧字段不参与启动。没有 `proxy_name` 的更老配置需补上代理名。原 `listen_host` / `listen_port` 已由 frpc 的本地目标取代。
4. 将旧 `shelldeck-frp` 命令移到备份目录，执行 `python3 scripts/install.py --config 原JSON路径`，然后直接运行 `shelldeck`。原控制 socket 路径不变时，CLI/MCP 无需重新配置。
5. 用 `quit` 退出，确认 ShellDeck 端口释放、不会自行重启，frpc PID 与配置不变；再次运行 `shelldeck` 并让客户端重新连接，验证来源、命令和文件传输。

旧管理脚本已移除。回滚需同时恢复旧脚本、配置和对应启动项；若回退到合并托管版本，先停止独立 frpc，避免重复托管。无须卸载系统上的 tmux，其他项目可能仍在使用。

## 验证

```sh
python3 -m unittest discover -s tests -p test_launcher.py -v
python3 -m unittest discover -s tests -p test_frp_deployment.py -v
SHELLDECK_FRPC=/absolute/path/to/frpc SHELLDECK_FRPS=/absolute/path/to/frps \
  python3 -m unittest discover -s tests -p test_frp.py -v
```

启动测试使用真实 PTY、TCP 和 Unix socket，验证生成命令、复用旧配置、动态读取代理目标、错误配置与冲突参数拒绝、重复启动隔离、退出与终端挂断。真实 frp 测试验证来源、命令、文件往返，以及 ShellDeck 退出后 frpc 的 PID 和配置保持不变、再次启动后重新连接。部署后还需通过 VPS 的真实入口验证；端口开放或 frpc 登录成功不等于完整验收。
