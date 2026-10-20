"""Read shared ShellDeck settings and, in remote mode, the existing frpc target."""

import ipaddress
import json
import os
from pathlib import Path
import re


def config_home():
    value = os.environ.get("XDG_CONFIG_HOME", "")
    return Path(value) if value and Path(value).is_absolute() else Path.home() / ".config"


def default_config():
    current = config_home() / "shelldeck/config.json"
    legacy = config_home() / "shelldeck-frp/service.json"
    return legacy if not current.exists() and legacy.exists() else current


def read_config(path=None, *, remote=True):
    explicit = path is not None
    path = Path(path if explicit else default_config()).expanduser().resolve()
    try:
        content = path.read_text()
    except FileNotFoundError:
        if explicit or remote:
            raise
        content = "{}"
    config = json.loads(content)
    if not isinstance(config, dict):
        raise ValueError("ShellDeck configuration must be a JSON object")

    def value(key):
        item = config.get(key)
        if not isinstance(item, str) or not item or "\x00" in item:
            raise ValueError(f"{key} must be a nonempty string")
        return item

    def local_path(item):
        target = Path(item).expanduser()
        return target if target.is_absolute() else path.parent / target

    agent_socket = local_path(value("agent_socket")) if "agent_socket" in config else config_home() / "shelldeck/agent/control.sock"
    settings = {"agent_socket": str(agent_socket)}
    if not remote:
        return settings

    frpc = local_path(value("frpc_config"))
    content = frpc.read_text()
    if frpc.suffix.lower() == ".json":
        proxies = json.loads(content)
    else:
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib
            except ImportError:
                raise ValueError("TOML needs Python 3.11+ or: python -m pip install -r requirements-frp.txt") from None
        proxies = tomllib.loads(content)
    if not isinstance(proxies, dict) or not isinstance(proxies.get("proxies"), list):
        raise ValueError("frpc_config must contain a proxies array; use the file defining the proxy")
    name = value("proxy_name")
    selected = [proxy for proxy in proxies["proxies"] if isinstance(proxy, dict) and proxy.get("name") == name]
    if len(selected) != 1:
        raise ValueError("proxy_name must match exactly one proxy in frpc_config")
    if "start" in proxies:
        start = proxies["start"]
        if not isinstance(start, list) or any(not isinstance(item, str) for item in start):
            raise ValueError("frpc start must be an array of proxy names")
        if start and name not in start:
            raise ValueError("selected proxy is excluded by frpc start")
    proxy = selected[0]
    if proxy.get("plugin"):
        raise ValueError("selected proxy must connect directly without a plugin")
    transport = proxy.get("transport", {})
    if proxy.get("type") != "tcp" or not isinstance(transport, dict) or transport.get("proxyProtocolVersion") != "v1":
        raise ValueError("selected proxy must use type=tcp and transport.proxyProtocolVersion=v1")
    host = proxy.get("localIP")
    try:
        if not isinstance(host, str):
            raise ValueError()
        address = ipaddress.IPv4Address(host)
        if not address.is_loopback:
            raise ValueError()
    except ValueError:
        raise ValueError("selected proxy localIP must be a literal IPv4 loopback address") from None
    port = proxy.get("localPort")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("selected proxy localPort must be an integer from 1 to 65535")
    remote_port = proxy.get("remotePort")
    endpoint = "VPS endpoint from the frpc configuration"
    if type(remote_port) is int and 1 <= remote_port <= 65535:
        server = proxies.get("serverAddr")
        # Included proxy fragments and environment templates may not expose a
        # static server address. Never render arbitrary config text in the TTY.
        if not isinstance(server, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", server):
            server = "<VPS IPv4>"
        endpoint = f"{server}:{remote_port}"
    return {**settings, "host": str(address), "port": port, "proxy_protocol": "v1",
            "client_endpoint": endpoint}
