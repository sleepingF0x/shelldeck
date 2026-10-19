# frp 部署与配置维护

配置和启动命令见 [README](../README.md#frp-config)。本机服务管理器为 [scripts/shelldeck_frp.py](../scripts/shelldeck_frp.py)，VPS 白名单程序为 [examples/frp/allowlist.py](../examples/frp/allowlist.py)。两者的服务器地址、端口和环境路径均由配置提供。

## 安装前提与顺序

frp 是独立程序，需从 [官方发布页](https://github.com/fatedier/frp/releases) 获取与操作系统和 CPU 架构匹配的二进制文件。两端使用同一版本，配置示例对应 frp 0.71.1。

1. **准备 VPS**：安装 frps；白名单脚本另需 Python 3 和 nftables，本文使用 systemd 管理服务。配置 frps、token、TLS 证书和 access.json，先应用白名单，再启动 frps。VPS 无需安装 ShellDeck。
2. **准备本机**：安装 frpc、Python 3.9+ 和 tmux，复制可信证书，填写 frpc.toml 和 service.json。本机服务管理器使用 macOS launchd。
3. **生成本机启动配置**：运行 `check` 和 `install`。`install` 校验本机配置、生成管理命令与 LaunchAgent，不下载 frp、不连接 VPS 安装文件，也不启动服务。
4. **建立连接**：确认 VPS 上的 frps 正在运行后，执行本机 `start`；它通过 SSH 刷新白名单，然后启动 ShellDeck 和 frpc。frpc 代理启动成功后，客户端才连接 VPS 的回连端口。

已有 frps 可复用，无需每次运行都安装。证书、token、控制端口和允许的代理端口必须与本机配置一致。手工配置及启动命令见 [frp 使用说明](frp.md)。

## 配置归属

| 文件 | 作用 |
| --- | --- |
| 本机 `service.json` | SSH 目标、程序路径、ShellDeck 监听与 socket、tmux、LaunchAgent、命令入口、日志和环境变量 |
| 本机 `frpc.toml` | VPS 地址、控制端口、token、可信证书、本地地址及转发端口 |
| VPS `frps.toml` | 监听地址、端口、允许申请的代理端口、token、证书和私钥 |
| VPS `access.json` | 本机来源、额外回连来源以及需要过滤的两个端口 |
| VPS systemd 单元 | 该机器上的 frps/helper 路径、配置路径、服务账户及启动依赖 |

frp 原生 TOML 和 systemd 单元本身属于部署配置。不要把其中的地址或路径复制回 Python 代码。`service.json` 可通过 `--config` 选择；相对路径以配置所在目录为基准，`~` 展开为本机用户目录。远端命令数组里的路径在 VPS 上使用，建议填写绝对路径。

管理器的 `install` 根据配置生成本机命令入口和 LaunchAgent。启动文件需要的绝对路径在安装时生成，配置更改后重新运行 `install`。配置中使用命令名时，安装环境应能找到该命令；未设置 `environment.PATH` 时，生成的启动项会保留这些程序所在目录。显式设置了该变量时，使用配置给定的搜索路径。填写绝对程序路径也可以避免登录环境差异。

## VPS 白名单

复制 [access.json 示例](../examples/frp/access.json) 到 VPS 私有配置目录，替换占位地址：

- `local_source`：本机公网出口 IPv4，允许访问控制端口和回连端口。
- `callback_sources`：其他明确允许回连的 IPv4/CIDR，仅允许访问回连端口。
- `control_port`：必须与 frps `bindPort`、frpc `serverPort` 一致。
- `callback_port`：必须与 frps `allowPorts` 和 frpc `remotePort` 一致。

在 VPS 上使用实际安装位置设置两个变量，显式指定配置路径运行：

```sh
# 示例位置，按该 VPS 的部署配置调整
SHELLDECK_ALLOWLIST_SCRIPT=/usr/local/libexec/shelldeck-frp/allowlist.py
SHELLDECK_ACCESS_CONFIG=/etc/shelldeck-frp/access.json
python3 "$SHELLDECK_ALLOWLIST_SCRIPT" --config "$SHELLDECK_ACCESS_CONFIG" apply
python3 "$SHELLDECK_ALLOWLIST_SCRIPT" --config "$SHELLDECK_ACCESS_CONFIG" status
```

白名单脚本需要 root。配置应归 root 所有、权限 `0600`，所在目录不可被普通用户写入。`refresh-local` 从已认证 SSH 会话的 `SSH_CONNECTION` 读取来源；通过本机 `refresh-access` 调用即可。它只更新 `local_source`，保留额外来源、端口和其他配置字段；同一个 nft transaction 原子替换专用表，不产生临时放行窗口。配置保存失败会恢复之前的过滤规则。

本机 `remote_allowlist_command` 应是包含远端 Python、脚本、`--config`、配置路径的数组。它按参数逐项 shell quoting 后交给 SSH，路径中的空格和引号不会改变命令结构。`access-status` 也使用同一配置。

独立 `inet shelldeck_frp` 输入链优先级 `-10`，只过滤配置指定的两个 TCP 端口。这使拒绝规则先于已有 iptables INPUT 放行规则生效。无需启用会加载全局规则的 nftables 服务，不要运行 `nft flush ruleset`。

VPS 的白名单 systemd 单元应使用相同参数：

```ini
# 以下为示例配置路径，需要按该 VPS 的实际位置填写。
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/libexec/shelldeck-frp/allowlist.py --config /etc/shelldeck-frp/access.json apply
ExecReload=/usr/bin/python3 /usr/local/libexec/shelldeck-frp/allowlist.py --config /etc/shelldeck-frp/access.json apply
RemainAfterExit=yes
UMask=0077
```

frps 单元使用非登录服务账户，并通过 `Requires`/`After` 依赖白名单服务，保证先过滤、后监听。修改远端脚本或配置位置时，同步更新该 systemd 单元和本机 `remote_allowlist_command`；运行 `systemd-analyze verify`、`systemctl daemon-reload` 后重新加载白名单服务。修改端口前先停用对应监听，再同步修改 frps、frpc 和 access 配置，避免切换时旧监听失去过滤。

## 本机运行

准备好私有配置、frpc 和 tmux 后，从仓库运行：

```sh
python3 scripts/shelldeck_frp.py --config "$HOME/.config/shelldeck-frp/service.json" check
python3 scripts/shelldeck_frp.py --config "$HOME/.config/shelldeck-frp/service.json" install
```

随后使用 `command_path` 指定的入口。示例入口为：

```sh
~/.local/bin/shelldeck-frp start
~/.local/bin/shelldeck-frp status
~/.local/bin/shelldeck-frp attach
~/.local/bin/shelldeck-frp access-status
~/.local/bin/shelldeck-frp refresh-access
~/.local/bin/shelldeck-frp stop
```

`install` 不启动服务；`start` 校验 frpc 配置、通过 SSH 更新本机来源后启动服务。启动项与当前配置不一致时会报错，先重新 `install`。更换配置中的路径、启动项或仓库位置时，先用旧配置 `stop`，再生成并启动新配置。修改启动项文件路径后应移走旧文件，避免下次登录时重复启动。

控制台在配置指定的专用 tmux 服务和会话中。按 `Ctrl-b`、`d` 退出查看，服务继续运行。ShellDeck 保留默认 900 秒无操作断开设置。本机服务在用户登录时启动；退出登录、休眠或断网会影响连接。frpc 恢复隧道后客户端重新连接，不恢复旧会话。

## DDNS 与安全边界

本机主动连接 VPS，因此通常不需要 DDNS。动态公网出口影响白名单，通过已有 SSH 刷新即可；DDNS 本身不会同步防火墙。VPS 地址变化时，应更新 frpc 地址、frps 绑定地址及证书 SAN。

frpc↔frps 隧道使用 token 和 TLS，frpc 校验 VPS 证书。客户端↔VPS 的回连端口仍为 ShellDeck 原始 TCP，没有额外 TLS；需要该段加密时让客户端经 VPN 或 SSH 隧道接入。IP 白名单以公网出口为单位，不能区分同一 NAT 后的主机，frp token 也不是回连客户端的身份认证。

本机 Agent socket 的父目录应为 `0700`、socket 为 `0600`；frpc 配置中的 token 和私有环境文件保持 `0600`。证书续签后通过可信 SSH 更新本机信任文件，再重启对应服务。

## 停用与回滚

先停止本机服务及 VPS frps，再停用白名单服务，最后删除专用 nft 表。VPS 的服务名和安装路径以该环境的配置为准：

```sh
~/.local/bin/shelldeck-frp stop
# 下列命令在 VPS 上执行，服务名按实际 systemd 配置填写
systemctl disable --now shelldeck-frps.service
systemctl disable --now shelldeck-frp-firewall.service
nft delete table inet shelldeck_frp
```

如需跨登录持续停用本机启动项，停止后将 `launch_agent_path` 指向的文件移到私有备份目录。保留配置即可恢复。回滚仅恢复本服务的文件和专用 nft 表；不要全量覆盖其他服务的防火墙配置。

## 验证

```sh
python3 -m unittest discover -s tests -p test_frp_deployment.py -v
python3 -m unittest discover -s tests -p test_deployment_download.py -v
```

部署测试使用不同路径、SSH 别名和端口，检查带引号路径、生成入口、配置缺失时停止、白名单端口、原子更新以及写入失败恢复。下载测试仅连接临时 loopback HTTP 服务并执行无害测试脚本。真实 frp 验证方式见 [frp 使用说明](frp.md#验证)。
