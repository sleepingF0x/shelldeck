# 接入独立部署的 frp

frp 是独立的转发工具，按[官方安装指引](https://gofrp.org/zh-cn/docs/setup/)部署。VPS 运行 frps，本机运行 frpc；它们的版本、配置文件、证书、鉴权、启动项和访问限制由该部署维护。Linux 的常驻管理可参考[官方 systemd 示例](https://gofrp.org/zh-cn/docs/setup/systemd/)，其他平台使用已有的服务管理方式。

ShellDeck 接收 frpc 转发过来的 TCP 连接，并读取 PROXY v1 来源信息。项目只提供这一接入约定和一个代理片段，不提供 frp 安装器、系统服务模板或 VPS 防火墙管理程序。VPS 无需安装 ShellDeck。

```text
客户端 Bash ←→ VPS frps :9099 ←→ 本机 frpc ←→ ShellDeck 127.0.0.1:19000
```

## frpc 代理

将 [proxy.toml](../examples/frp/proxy.toml) 中的片段加入现有 frpc 配置，由原 frpc 服务加载：

```toml
[[proxies]]
name = "shelldeck"
type = "tcp"
localIP = "127.0.0.1"
localPort = 19000
remotePort = 9099
transport.proxyProtocolVersion = "v1"
```

保留原 frpc 的服务器地址、控制端口、鉴权和 TLS 设置。该片段不是完整的 frpc 配置；VPS 的 frps 需要允许代理使用 `9099`，访问白名单由 VPS 的防火墙或安全组维护。本机主动连接 VPS，通常不需要 DDNS；本机出口变化后由原部署方式更新访问限制。

三个关键字段：

| 字段 | 接入约定 |
| --- | --- |
| `localIP` / `localPort` | ShellDeck 的本地监听目标，示例为 `127.0.0.1:19000` |
| `remotePort` | 客户端连接的 VPS 端口，示例为 `9099` |
| `transport.proxyProtocolVersion` | 必须为 `v1`，让 ShellDeck 识别 VPS 看到的来源 IP 和端口 |

## ShellDeck 配置

将 [shelldeck.json](../examples/shelldeck.json) 保存为 `~/.config/shelldeck/config.json`，填写现有 frpc 配置的真实位置与代理名：

```json
{
  "frpc_config": "~/.config/frp/frpc.toml",
  "proxy_name": "shelldeck",
  "agent_socket": "~/.config/shelldeck/agent/control.sock"
}
```

`frpc_config` 支持静态 TOML/JSON，所选代理必须是唯一匹配名称的 TCP 代理，显式填写 IPv4 回环 `localIP`、有效整数 `localPort` 和 PROXY v1，且不使用插件。被非空 `start` 排除的代理会被拒绝。ShellDeck 只读取本地连接参数，不修改文件或操作 frp 进程。

文件路径支持 `~`，相对路径按 ShellDeck JSON 所在目录解析。代理名称、地址、端口和协议须为静态值；其他字段中带引号的环境模板可以保留。任意 Go 模板循环/条件、INI/YAML 不支持。frpc 使用 `includes` 时，可将 `frpc_config` 指向定义该代理的静态片段。

Python 3.11+ 使用标准库读取 TOML；Python 3.9/3.10 先用同一解释器安装 `requirements-frp.txt`。未填写 `agent_socket` 时使用配置根目录下的 `shelldeck/agent/control.sock`，CLI/MCP 连接这个 socket。

在仓库目录安装一次命令，然后按需启动：

```sh
python3 scripts/install.py
shelldeck
```

安装器优先读取 `$XDG_CONFIG_HOME/shelldeck/config.json`；不存在时兼容已有的 `$XDG_CONFIG_HOME/shelldeck-frp/service.json`。未设置有效的绝对 `XDG_CONFIG_HOME` 时使用 `~/.config`。已有配置无需复制，旧配置中的进程管理和 SSH 字段不参与启动。也可用 `--config /absolute/path/to/config.json` 指定文件；显式指定的文件无效时直接报错，不回退其他配置。

安装器只生成 `~/.local/bin/shelldeck`。确保该目录在 `PATH` 中，缺少时在自己的 shell 配置中加入 `export PATH="$HOME/.local/bin:$PATH"`。已有非本项目生成的同名命令不会被覆盖，可通过 `--command-path` 指定其他位置。移动仓库或更换 Python 后重新安装命令。

ShellDeck 在当前交互终端运行。`ls` 查看会话，`i ID` 进入会话，Ctrl+] 返回菜单，菜单中的 `quit` 退出。退出后 frp 继续运行，再次启动后客户端重新连接。每次启动重新读取代理配置，与其冲突的 `-H` / `-p` 会报错。修改目标时先让 frpc 加载原配置的变更，再重启 ShellDeck。

原有直连命令 `python3 shelldeck.py -H ... -p ...` 继续可用。已有旧后台 ShellDeck 实例需先停止，以便前台实例使用监听端口和控制 socket。删除仓库中的部署工具不会删除系统中已安装的 frp、启动项或白名单，这些由实际环境继续维护。

若旧 frpc 启动参数或 ShellDeck 的 `frpc_config` 指向仓库内的完整 frpc 示例，升级前先将该配置复制到项目外的 frp 配置目录，保留原鉴权、TLS 文件和运行环境，同步更新这两处路径并确认可用。本次移除了完整 frpc 示例；新的 `proxy.toml` 只是代理片段，不能直接替代完整配置。

## 来源地址与连接边界

- 名称、备注、CLI/MCP 和执行历史使用 **VPS 看到的客户端出口 IPv4 地址**。同一 NAT 后的多台机器可能共用 IP 和名称，但会话 ID 各自独立。
- 当前只接受 PROXY v1 的 `TCP4`。`TCP6`、`UNKNOWN`、v2、畸形或缺失的头不会创建会话。客户端应连接 VPS 的 IPv4 地址。
- frpc 与 ShellDeck 须在同机、同一网络命名空间。ShellDeck 监听 IPv4 回环地址，只接受回环连接；容器部署需要另行安排网络，不能直接把这个监听器暴露到公网。
- 来源信息由受信任的本机 frpc 提供；PROXY protocol 本身不认证远端主机，同机其他进程也能连接回环端口。客户端到 VPS 仍是原始 TCP；frp 隧道的 TLS 只覆盖 frpc 与 frps 之间。
- 每条连接须在接收后 5 秒内发送完整头；头最多 107 字节，等待头的连接与在线会话合计受 `--max-sessions` 限制。等待头不会阻塞已有会话或 Agent 接口，`close all` 和退出会清理这些连接。
- ShellDeck 退出或隧道真正断开会结束受影响的连接；网络故障的发现时间取决于 TCP/frp 心跳。恢复后客户端重新连接，不恢复旧会话或断点续传；未确认的命令和传输结果保持未知，不自动重发。
- ShellDeck 的默认 900 秒空闲超时独立生效，frp 心跳不会延长它。长任务可用 `shelldeck -t 0` 或更大的超时。

## 验证

启动和协议测试不需要安装 frp：

```sh
python3 -m unittest discover -s tests -p test_launcher.py -v
python3 -m unittest discover -s tests -p test_proxy_protocol.py -v
```

真实 frp 测试从 `PATH` 查找 `frpc`、`frps` 和 `openssl`，也可指定程序路径。它在临时目录构造两端配置、随机凭据和证书，将项目中的代理片段加入测试配置，只使用回环地址和临时端口。测试不操作现有系统服务或 VPS，缺少工具时明确跳过。该测试已按 frp 0.71.1 验证。

```sh
SHELLDECK_FRPC=/absolute/path/to/frpc \
SHELLDECK_FRPS=/absolute/path/to/frps \
  python3 -m unittest discover -s tests -p test_frp.py -v
```

真实链路测试覆盖来源 IP/端口、会话隔离、TLS 与 token 拒绝、二进制文件往返、隧道断开，以及 ShellDeck 前台退出后 frpc 继续运行、重新启动后再次接入。

首次接入已有 VPS 后，还需从允许的来源真实回连：核对来源 IP，执行一个无害命令，上传下载临时文件并核对 SHA-256，再退出和重新启动 ShellDeck 后验证新连接。只看到端口开放或 frpc 登录成功不能替代这组验收。访问白名单由 frp 的部署环境另外验证。

参考：[frp 代理参数](https://gofrp.org/zh-cn/docs/reference/proxy/)、[TLS 证书验证](https://gofrp.org/zh-cn/docs/features/common/network/network-tls/)、[配置校验](https://gofrp.org/zh-cn/docs/features/common/configure/)。
