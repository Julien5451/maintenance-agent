"""Project-only TCP relay to an existing, credential-free Windows loopback HTTP proxy.

Run --inspect with Docker permission, then run --serve as the normal WSL user.
The relay binds only the project's Docker bridge gateway and accepts project peers.
It never logs traffic, credentials, host proxy values, or model service addresses.
"""

import argparse
import asyncio
import ipaddress
import json
import os
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY = ROOT / "reports" / "network_fix_topology.json"
NETWORK = "maintenance-agent_default"


def existing_proxy():
    raw = next(
        (
            os.environ[key]
            for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
            if os.environ.get(key)
        ),
        "",
    )
    proxy = urlsplit(raw)
    safe = (
        proxy.scheme == "http"
        and proxy.hostname in {"localhost", "127.0.0.1", "::1"}
        and proxy.port is not None
        and not proxy.username
        and not proxy.password
        and not proxy.query
        and not proxy.fragment
        and proxy.path in {"", "/"}
    )
    if not safe:
        raise ValueError("需要现有、无认证信息的本机回环HTTP代理；不接受凭证或远程代理")
    return proxy


def inspect_network():
    proxy = existing_proxy()
    with socket.create_connection((proxy.hostname, proxy.port), timeout=5):
        pass
    raw = subprocess.run(
        ["docker", "network", "inspect", NETWORK], check=True, capture_output=True, text=True
    ).stdout
    network = json.loads(raw)[0]
    ipam = next(item for item in network["IPAM"]["Config"] if ":" not in item["Subnet"])
    listeners = subprocess.run(
        ["ss", "-H", "-ltn"], capture_output=True, text=True, check=True
    ).stdout
    wsl_listener = any(
        len(line.split()) > 3 and line.split()[3].endswith(":" + str(proxy.port))
        for line in listeners.splitlines()
    )
    result = {
        "timestamp": datetime.now(UTC).isoformat(),
        "network": NETWORK,
        "gateway": ipam["Gateway"],
        "subnet": ipam["Subnet"],
        "relay_port": 18081,
        "proxy_credentials_present": False,
        "wsl_can_connect_host_proxy": True,
        "wsl_proxy_listener_present": wsl_listener,
    }
    TOPOLOGY.parent.mkdir(parents=True, exist_ok=True)
    TOPOLOGY.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "ready",
                "topology_report": str(TOPOLOGY),
                "gateway": result["gateway"],
                "relay_port": 18081,
            },
            ensure_ascii=False,
        )
    )


async def serve():
    topology = json.loads(TOPOLOGY.read_text(encoding="utf-8"))
    subnet = ipaddress.ip_network(topology["subnet"])
    gateway = ipaddress.ip_address(topology["gateway"])
    if gateway not in subnet or gateway.is_loopback or not gateway.is_private:
        raise ValueError("中继必须绑定本项目Docker私有网关")
    proxy = existing_proxy()
    semaphore = asyncio.Semaphore(32)
    counters = {"accepted": 0, "rejected": 0, "upstream_failures": 0}

    def record_statistics():
        path = ROOT / "reports" / "network_fix_relay_stats.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"timestamp": datetime.now(UTC).isoformat(), "network": NETWORK, **counters}
            ),
            encoding="utf-8",
        )
        temporary.replace(path)

    record_statistics()

    async def pump(reader, writer):
        while data := await asyncio.wait_for(reader.read(65536), timeout=300):
            writer.write(data)
            await writer.drain()

    async def handle(reader, writer):
        peer = writer.get_extra_info("peername")
        upstream = None
        tasks = []
        try:
            if not peer or ipaddress.ip_address(peer[0]) not in subnet:
                counters["rejected"] += 1
                return
            async with semaphore:
                counters["accepted"] += 1
                record_statistics()
                up_reader, upstream = await asyncio.wait_for(
                    asyncio.open_connection(proxy.hostname, proxy.port), timeout=10
                )
                tasks = [
                    asyncio.create_task(pump(reader, upstream)),
                    asyncio.create_task(pump(up_reader, writer)),
                ]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        except (OSError, TimeoutError):
            counters["upstream_failures"] += 1
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for connection in (writer, upstream):
                if connection is not None:
                    connection.close()
                    try:
                        await connection.wait_closed()
                    except OSError:
                        pass
            record_statistics()

    server = await asyncio.start_server(handle, str(gateway), topology["relay_port"])
    print("模拟业务演示：中继仅监听本项目Docker网关，使用已核实的本机无凭证代理", flush=True)
    try:
        async with server:
            await server.serve_forever()
    finally:
        print(json.dumps({"relay_statistics": counters}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--inspect", action="store_true")
    group.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    try:
        if args.inspect:
            inspect_network()
        else:
            asyncio.run(serve())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        # Never emit a raw exception: it might contain source proxy details.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
