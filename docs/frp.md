# 本机运行 ShellDeck，VPS 使用 frp 转发

本机运行 ShellDeck、frpc、CLI 和 MCP，VPS 只运行 frps。客户端连接 VPS 的 TCP 端口，frpc 将每个连接独立转发到本机，并在连接开头附带 VPS 看到的客户端来源 IPv4 地址和端口。

```text
客户端 Bash ←→ VPS frps :9099 ←→ 本机 frpc ←→ ShellDeck 127.0.0.1:19000
                              ↑
                  本机 frpc 主动连接 VPS :7000（TLS）
```

| 位置 | 程序或文件 | 用途 |
| --- | --- | --- |
| VPS | frps、[frps.toml](../examples/frp/frps.toml) | 7000 接收 frpc 隧道，9099 接收客户端 |
| 本机 | frpc、[frpc.toml](../examples/frp/frpc.toml) | 连接 VPS，并将会话转发到本机 19000 |
| 本机 | ShellDeck | 解析来源地址、管理会话、保存名称和历史 |

两个 frp 程序使用同一版本。本示例使用 TOML，对应 frp 0.71.1。ShellDeck 核心直连模式无需第三方库；共享 TOML 配置在 Python 3.9/3.10 下需要 `requirements-frp.txt`。VPS 无需部署 ShellDeck，也不保存它的名称库、命令历史或传输文件。

## 0. 安装 frp

先从 [frp 官方发布页](https://github.com/fatedier/frp/releases) 下载匹配操作系统和 CPU 架构的压缩包。解压后，VPS 使用其中的 `frps`，本机使用对应平台包中的 `frpc`；将程序放到各自配置指定的路径。下文直接使用 `frps`、`frpc` 命令，要求所在目录已加入 `PATH`，也可替换为实际程序路径。

ShellDeck 不附带或自动安装这两个程序，`scripts/install.py` 只生成前台启动命令。准备好下文的鉴权、证书和端口白名单后，先启动 VPS 上的 frps，再启动本机 ShellDeck 和 frpc。使用白名单脚本及系统服务管理时，按 [部署说明](frp-deployment.md#安装前提与顺序) 操作。

## 1. 准备鉴权和 TLS

两端需要同一个随机 token。VPS 需要服务器证书和私钥，本机需要对应的 CA 证书。证书的 SAN 必须包含 `SHELLDECK_FRP_SERVER_ADDR` 使用的域名或 IP；本机配置会验证证书，不能只改成一个未包含在 SAN 中的地址。

已有合适的证书时直接使用。没有证书时，可以在 VPS 为本次部署新建一个独立目录，生成自签名证书并让本机明确信任这张证书。以下示例使用域名 `frp.example.net`，应先换成 VPS 的实际域名；域名需解析到 VPS。

```sh
umask 077
mkdir -p "$HOME/.config/shelldeck-frp"
export SHELLDECK_FRP_SERVER_ADDR="frp.example.net"
SHELLDECK_FRP_TLS_DIR=$(mktemp -d "$HOME/.config/shelldeck-frp/tls.XXXXXX")
export SHELLDECK_FRP_CERT_FILE="$SHELLDECK_FRP_TLS_DIR/server.crt"
export SHELLDECK_FRP_KEY_FILE="$SHELLDECK_FRP_TLS_DIR/server.key"
openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days 365 \
  -subj "/CN=$SHELLDECK_FRP_SERVER_ADDR" \
  -addext "subjectAltName=DNS:$SHELLDECK_FRP_SERVER_ADDR" \
  -keyout "$SHELLDECK_FRP_KEY_FILE" -out "$SHELLDECK_FRP_CERT_FILE"
openssl rand -hex 32 > "$SHELLDECK_FRP_TLS_DIR/frp.token"
export SHELLDECK_FRP_TOKEN="$(cat "$SHELLDECK_FRP_TLS_DIR/frp.token")"
```

若使用 IPv4 地址连接，将 SAN 参数改成 `subjectAltName=IP:实际VPS地址`。生成命令需要支持 `-addext` 的 OpenSSL/LibreSSL。证书有效期为 365 天，到期前需更新证书；自签名证书更新后也要更新本机信任文件。

通过已有的可信 SSH/SFTP 通道，将 `server.crt` 和 `frp.token` 复制到本机的私有目录，并将 token 文件权限设为 `0600`。私钥 `server.key` 留在 VPS。若使用 CA 签发的证书，本机复制 CA 证书，VPS 的 `certFile` 应包含所需证书链。

## 2. 在 VPS 启动 frps

把 `examples/frp/frps.toml` 复制为 VPS 工作目录下的 `frps.toml`。在设置好上一节环境变量的终端执行：

```sh
: "${SHELLDECK_FRP_TOKEN:?请设置共享 token}"
: "${SHELLDECK_FRP_CERT_FILE:?请设置服务器证书绝对路径}"
: "${SHELLDECK_FRP_KEY_FILE:?请设置服务器私钥绝对路径}"
frps verify -c ./frps.toml
frps -c ./frps.toml
```

示例只允许 frpc 申请 9099 端口，要求隧道使用 TLS，并通过 token 验证 frpc。VPS 的防火墙或安全组需要允许本机访问 TCP 7000、允许预期客户端访问 TCP 9099。7000 是 frp 控制入口，客户端 Bash 连接的是 9099。

## 3. 在本机启动 ShellDeck 和 frpc

下面是首次排查时的手工启动方式。日常让 frpc 由独立系统服务常驻，按 [README](../README.md#frp-start) 安装一次 `shelldeck` 命令，用时直接运行。已有常驻 frpc 时跳过下面的 frpc 启动命令。共享配置方式自动读取现有代理的本地目标和协议，无需再次填写监听参数。

先在项目目录的交互终端启动 ShellDeck：

```sh
python3 shelldeck.py -H 127.0.0.1 -p 19000 --proxy-protocol v1 \
  --agent-socket "$HOME/.config/shelldeck/agent/control.sock"
```

ShellDeck 仍需要交互终端。CLI 和 MCP 继续使用上面的本地 socket，注册方式见 [README](../README.md#agent-mcp)。名称库、历史库和文件传输的本机路径也都位于本机。

在另一个本机终端设置地址、token 和 CA 文件。下面的路径应替换为上一节复制到本机的实际路径：

```sh
export SHELLDECK_FRP_SERVER_ADDR="frp.example.net"
export SHELLDECK_FRP_CA_FILE="$HOME/.config/shelldeck-frp/server.crt"
export SHELLDECK_FRP_TOKEN="$(cat "$HOME/.config/shelldeck-frp/frp.token")"
: "${SHELLDECK_FRP_SERVER_ADDR:?请设置 VPS 地址}"
: "${SHELLDECK_FRP_CA_FILE:?请设置 CA 证书绝对路径}"
: "${SHELLDECK_FRP_TOKEN:?请设置共享 token}"
frpc verify -c examples/frp/frpc.toml
frpc -c examples/frp/frpc.toml
```

等 frpc 报告代理启动成功，再让客户端连接 VPS 的 IPv4 地址和 9099 端口。客户端仍使用原有 Bash/TCP 连接方式，只需将服务端地址换成 VPS；客户端不需要 frp、Python 或 PROXY protocol 支持。

配置中的 `transport.proxyProtocolVersion = "v1"` 必须与 ShellDeck 的 `--proxy-protocol v1` 一起启用。修改端口时，VPS 的 `allowPorts`、frpc 的 `remotePort` 和客户端连接端口需一致；frpc 的 `localPort` 与 ShellDeck 的 `-p` 需一致。

这些环境变量仅在当前终端中有效。以后交给进程管理器运行时，需要显式传入同一组变量和绝对路径；不要把实际 token 或私钥提交到仓库。

## 来源地址与连接边界

- `sessions`、名称、备注、CLI/MCP 和执行历史使用 PROXY 头中的来源地址，原数据库结构无需迁移。这里的来源是 **VPS 看到的客户端出口 IP**；同一 NAT 后的多台机器仍可能共用 IP 和名称，但会话 ID 各自独立。
- `--proxy-protocol v1` 当前接受 `TCP4`。`TCP6`、`UNKNOWN`、v2、畸形或缺失的头不会降级成代理地址，也不会创建会话。VPS 的客户端入口请使用 IPv4。
- PROXY 模式必须监听 IPv4 回环地址，只接受来自回环地址的连接，适用于 frpc 与 ShellDeck 在同机、同一网络命名空间运行。容器中的 frpc 需要另行规划网络；不要把该监听器通过 Docker 映射或其他端口转发暴露到公网。
- 来源信息由受信任的本机 frpc 提供；PROXY protocol 本身不认证远端主机，同机其他进程也能连接回环端口。客户端到 VPS 仍为现有的原始 TCP，隧道 TLS 只覆盖 frpc 与 frps 之间。
- 每条连接须在接收后 5 秒内发送完整头；头最多 107 字节，等待头的连接与在线会话合计受 `--max-sessions` 限制，默认最多 100 条。头通过校验后才分配会话 ID，后续字节保持原样。等待头不会阻塞已有会话或 Agent 接口，`close all` 和退出会清理这些连接。
- 本机退出、隧道真正断开或 frp 重启会结束受影响的连接。网络中断的检测时间取决于 TCP/frp 心跳，短暂丢包不保证立即显示断线。frpc 恢复隧道后，客户端需要重新连接并获得新 ID；没有会话恢复或断点续传。断开前未确认的命令/传输结果仍为未知，不自动重发。
- ShellDeck 的 900 秒空闲超时仍然生效，frp 心跳不延长它；长任务按需设置 `-t 0` 或更大的超时。

关闭代理模式时，停止 frpc、退出 ShellDeck，再不带 `--proxy-protocol` 启动原来的直连监听器即可。切换会断开现有连接，名称和历史数据无需回滚或清理。直连模式不会识别或剥离形似 PROXY 头的普通输出。

## 验证

不安装 frp 也能运行协议与应用集成测试，覆盖拆包、粘包、来源隔离、历史、MCP、文件往返、超时和连接上限：

```sh
.venv-mcp/bin/python -m unittest discover -s tests -p test_proxy_protocol.py -v
```

真实 frp 测试从 `PATH` 查找 `frpc`、`frps` 和 `openssl`，也可指定二进制路径。它只使用回环地址、临时端口、临时证书和临时目录，不访问 VPS，不更改系统服务。缺少工具时该组测试会明确跳过；只有实际运行成功才算链路验收。

```sh
SHELLDECK_FRPC=/absolute/path/to/frpc \
SHELLDECK_FRPS=/absolute/path/to/frps \
  .venv-mcp/bin/python -m unittest discover -s tests -p test_frp.py -v
```

VPS 部署后另行验收：两个客户端同时连接时分别核对来源 IP/端口和会话 ID；给其中一个执行命令确认另一个未收到；上传再下载二进制文件核对摘要；停止本机 frpc，确认原会话断开、未确认任务不报成功，再启动 frpc 并让客户端重新连接。单独看到端口开放或 frpc 登录成功，不能替代这组检查。

参考：[frp 代理参数](https://gofrp.org/zh-cn/docs/reference/proxy/)、[TLS 证书验证](https://gofrp.org/zh-cn/docs/features/common/network/network-tls/)、[配置校验](https://gofrp.org/zh-cn/docs/features/common/configure/)、[PROXY protocol 规范](https://www.haproxy.org/download/2.9/doc/proxy-protocol.txt)。
