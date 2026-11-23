"""Single-host WebSocket traffic probe; not a substitute for real school Wi-Fi testing."""
import asyncio
import json
import time

from aiohttp.test_utils import TestClient, TestServer
from server import create_app


async def run(students=70, seconds=5, hz=4):
    server = TestServer(create_app("benchmark-secret"))
    client = TestClient(server)
    await client.start_server()
    sockets, listen_tasks = [], []
    counts = [0] * students
    received_rows = [0] * students

    async def reader(ws, i):
        while not ws.closed:
            try:
                msg = await ws.receive_json(timeout=seconds + 15)
                if msg.get("t") == "positions":
                    counts[i] += 1
                    received_rows[i] += len(msg["rows"])
            except (asyncio.TimeoutError, ConnectionError, RuntimeError, ValueError):
                return

    try:
        teacher = await client.ws_connect("/ws")
        sockets.append(teacher)
        await teacher.send_json({"t": "hello", "role": "teacher", "password": "benchmark-secret", "room": "1111"})
        assert (await teacher.receive_json())["t"] == "welcome"
        for i in range(students):
            ws = await client.ws_connect("/ws")
            sockets.append(ws)
            await ws.send_json({"t": "hello", "role": "student", "room": "1111"})
            assert (await ws.receive_json())["t"] == "welcome"
            await ws.send_json({"t": "put", "d": {"n": f"U{i:02d}", "c": 0, "x": 30 + i, "y": 400}})
            listen_tasks.append(asyncio.create_task(reader(ws, i)))

        await asyncio.sleep(.5)
        start = time.perf_counter()
        for tick in range(seconds * hz):
            for i, ws in enumerate(sockets[1:]):
                await ws.send_json({"t": "patch", "d": {"x": 100 + i + tick, "y": 400}})
            await asyncio.sleep(1/hz)
        await asyncio.sleep(.6)
        report = {
            "students": students, "duration_sec": seconds, "send_hz_per_student": hz,
            "attempted_patch_messages": students * seconds * hz,
            "position_frames_min": min(counts), "position_frames_max": max(counts),
            "position_rows_min": min(received_rows), "position_rows_max": max(received_rows),
            "elapsed_sec": round(time.perf_counter() - start, 2),
            "interpretation": "Same-host synthetic traffic; no school Wi-Fi or browser rendering guarantee"
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        assert min(received_rows) >= students * seconds * hz * .75
        return report
    finally:
        for task in listen_tasks:
            task.cancel()
        await asyncio.gather(*listen_tasks, return_exceptions=True)
        await asyncio.gather(*(ws.close() for ws in sockets), return_exceptions=True)
        await client.close()


if __name__ == "__main__":
    asyncio.run(run())
