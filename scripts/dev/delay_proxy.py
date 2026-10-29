#!/usr/bin/env python3
"""TCP delay proxy: adds a one-way delay (ms) to every chunk, both directions.

just --command python scripts/dev/delay_proxy.py <one-way ms> [--listen-port P] [--server-port P] [--server-host H]
"""

import argparse
import asyncio
import json
import os
from pathlib import Path


async def pump(reader, writer, delay):
    # Delay line: each chunk leaves DELAY after it arrived; sleeping between read and
    # write instead would queue delays behind each other and lag would grow with traffic.
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()

    async def writer_loop():
        while (item := await queue.get()) is not None:
            due, data = item
            await asyncio.sleep(max(0.0, due - loop.time()))
            writer.write(data)
            await writer.drain()

    task = asyncio.create_task(writer_loop())
    try:
        while data := await reader.read(65536):
            queue.put_nowait((loop.time() + delay, data))
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        queue.put_nowait(None)
        try:
            await task
        except ConnectionError:
            pass
        writer.close()


def main():
    parser = argparse.ArgumentParser(
        prog="just --command python scripts/dev/delay_proxy.py", description=__doc__
    )
    parser.add_argument("delay_ms", nargs="?", type=float, default=60.0)
    parser.add_argument("--listen-port", type=int, default=25600)
    parser.add_argument("--server-host", default="127.0.0.1")
    parser.add_argument("--server-port", type=int, default=25599)
    parser.add_argument(
        "--record",
        default=None,
        help="write {pid, listen_port} JSON here once listening (exact-id cleanup)",
    )
    args = parser.parse_args()
    delay = args.delay_ms / 1000

    async def handle(client_reader, client_writer):
        server_reader, server_writer = await asyncio.open_connection(
            args.server_host, args.server_port
        )
        await asyncio.gather(
            pump(client_reader, server_writer, delay),
            pump(server_reader, client_writer, delay),
        )

    async def serve():
        server = await asyncio.start_server(handle, "127.0.0.1", args.listen_port)
        if args.record:
            Path(args.record).write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "listen_port": args.listen_port,
                        "server": f"{args.server_host}:{args.server_port}",
                        "delay_ms": args.delay_ms,
                    }
                )
                + "\n"
            )
        print(
            f"proxy up: 127.0.0.1:{args.listen_port} -> {args.server_host}:{args.server_port}, "
            f"one-way delay {args.delay_ms}ms",
            flush=True,
        )
        async with server:
            await server.serve_forever()

    asyncio.run(serve())


if __name__ == "__main__":
    main()
