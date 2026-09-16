"""Fake core child for supervisor tests: heartbeats N times, then hangs / acks kills / exits.

Uses only the stdlib so the child does not depend on the package under test.
Args: --beats N (heartbeats before hanging, default forever), --interval S, --ack (answer sup.kill
with sup.ack and exit), --restart-ack (ack every sup.kill but exit 0 only for `mode=restart`,
simulating the core's clean shutdown so the supervisor respawns it), --stop-ack (exit 0 on
sup.stop, simulating a graceful NoxCore.stop(); when absent, sup.stop is received and ignored -
simulating a hung core), --boot-delay S (sleep before connecting at all, simulating a slow cold
boot), --exit-after S (exit with code 3 without connecting).

B-1: authenticates once per connection (`sup.auth` -> `sup.auth_ok`) before doing anything else;
every frame after that carries no token.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid


def envelope(name: str, kind: str, payload: dict[str, object], corr: str | None = None) -> bytes:
    msg = {
        "v": 1,
        "id": str(uuid.uuid4()),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "kind": kind,
        "name": name,
        "corr": corr,
        "src": {"role": "core", "id": "fake-core"},
        "payload": payload,
    }
    return (json.dumps(msg) + "\n").encode("utf-8")


async def authenticate(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, token: str
) -> bool:
    writer.write(
        envelope("sup.auth", "request", {"token": token, "role": "core", "pid": os.getpid()})
    )
    await writer.drain()
    line = await reader.readline()
    if not line:
        return False
    msg = json.loads(line)
    return bool(msg.get("name") == "sup.auth_ok")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--beats", type=int, default=10**9)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument("--ack", action="store_true")
    parser.add_argument("--restart-ack", action="store_true")
    parser.add_argument("--stop-ack", action="store_true")
    parser.add_argument("--boot-delay", type=float, default=0.0)
    parser.add_argument("--exit-after", type=float, default=None)
    args = parser.parse_args()

    if args.exit_after is not None:
        await asyncio.sleep(args.exit_after)
        return 3

    if args.boot_delay:
        await asyncio.sleep(args.boot_delay)  # a cold boot's imports, before the first heartbeat

    host = os.environ["NOX_SUPERVISOR_HOST"]
    port = int(os.environ["NOX_SUPERVISOR_PORT"])
    token = os.environ["NOX_SUPERVISOR_TOKEN"]
    reader, writer = await asyncio.open_connection(host, port)
    if not await authenticate(reader, writer, token):
        return 2

    async def read_loop() -> None:
        while True:
            line = await reader.readline()
            if not line:
                return
            msg = json.loads(line)
            name = msg.get("name")
            if name == "sup.kill" and (args.ack or args.restart_ack):
                writer.write(envelope("sup.ack", "response", {"ok": True}, corr=msg["id"]))
                await writer.drain()
                mode = str(msg.get("payload", {}).get("mode", ""))
                if args.ack or mode == "restart":
                    # --restart-ack exits only for mode=restart: a restart request is a clean
                    # shutdown the supervisor answers by respawning, not a kill switch.
                    await asyncio.sleep(0.05)
                    os._exit(0)
            elif name == "sup.stop" and args.stop_ack:
                # simulates NoxCore.stop() completing well inside the supervisor's budget
                await asyncio.sleep(0.05)
                os._exit(0)
            # sup.stop without --stop-ack is received and ignored: simulates a hung core that
            # misses NoxCore.stop()'s own budget, so the supervisor must terminate it.

    reader_task = asyncio.create_task(read_loop())
    for _ in range(args.beats):
        writer.write(envelope("sup.heartbeat", "event", {"pid": os.getpid()}))
        await writer.drain()
        await asyncio.sleep(args.interval)
    await asyncio.sleep(3600)  # hang: alive and connected, but silent
    reader_task.cancel()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
