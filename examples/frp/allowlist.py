#!/usr/bin/env python3
"""Manage only the shelldeck_frp nftables table using an explicit JSON config.

Install as root on the VPS. A refresh trusts the source of an authenticated
SSH connection; it never accepts a caller-supplied public-IP lookup result.
"""

import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import tempfile


TABLE = "shelldeck_frp"


def validate(config):
    local = str(ipaddress.IPv4Address(config["local_source"]))
    if not isinstance(config["callback_sources"], list):
        raise ValueError("callback_sources must be a list of IPv4 CIDRs")
    callbacks = sorted({str(ipaddress.IPv4Network(value, strict=True))
                        for value in config["callback_sources"]})
    ports = {key: config[key] for key in ("control_port", "callback_port")}
    if any(type(value) is not int or not 1 <= value <= 65535 for value in ports.values()):
        raise ValueError("control_port and callback_port must be integers from 1 to 65535")
    if ports["control_port"] == ports["callback_port"]:
        raise ValueError("control_port and callback_port must differ")
    return {**config, "local_source": local, "callback_sources": callbacks, **ports}


def rules(config):
    local = config["local_source"]
    callbacks = sorted(set(config["callback_sources"] + [local + "/32"]))
    return """table inet shelldeck_frp {
    set local_v4 { type ipv4_addr; flags interval; elements = { %s } }
    set callbacks_v4 { type ipv4_addr; flags interval; auto-merge; elements = { %s } }
    chain input {
        type filter hook input priority -10; policy accept;
        tcp dport %d ip saddr @local_v4 counter accept comment "shelldeck control allow"
        tcp dport %d counter drop comment "shelldeck control deny"
        tcp dport %d ip saddr @callbacks_v4 counter accept comment "shelldeck callback allow"
        tcp dport %d counter drop comment "shelldeck callback deny"
    }
}
""" % (local, ", ".join(callbacks), config["control_port"], config["control_port"],
       config["callback_port"], config["callback_port"])


def apply(config):
    tables = json.loads(subprocess.check_output(["nft", "-j", "list", "tables"]))
    exists = any(entry.get("table", {}).get("family") == "inet"
                 and entry.get("table", {}).get("name") == TABLE
                 for entry in tables["nftables"])
    transaction = ("delete table inet " + TABLE + "\n" if exists else "") + rules(config)
    subprocess.run(["nft", "-c", "-f", "-"], input=transaction, text=True, check=True)
    # Delete and recreate within one nft transaction: no unfiltered interval.
    subprocess.run(["nft", "-f", "-"], input=transaction, text=True, check=True)


def save(config, path):
    fd, temporary = tempfile.mkstemp(prefix=".access-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(config, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="VPS access JSON path")
    parser.add_argument("command", choices=("apply", "refresh-local", "status"))
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("run as root on the VPS")
    path = args.config.expanduser().resolve()
    with open(path.with_name(path.name + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = validate(json.loads(path.read_text()))
        config = dict(previous)
        if args.command == "refresh-local":
            connection = os.environ.get("SSH_CONNECTION", "").split()
            if len(connection) != 4:
                parser.error("refresh-local requires an authenticated SSH session")
            config["local_source"] = str(ipaddress.IPv4Address(connection[0]))
        if args.command != "status":
            apply(config)
            if config != previous:
                try:
                    save(config, path)
                except BaseException:
                    apply(previous)
                    raise
        print(json.dumps(config, indent=2), flush=True)
        if args.command == "status":
            subprocess.run(["nft", "list", "table", "inet", TABLE], check=True)


if __name__ == "__main__":
    main()
