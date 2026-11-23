"""Live WebSocket integration tests for the 70-player server."""
import asyncio
import unittest

from aiohttp.test_utils import TestClient, TestServer
from server import MAX_STUDENTS, create_app


async def message_of(ws, predicate, timeout=8):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        left = deadline - asyncio.get_running_loop().time()
        if left <= 0:
            raise AssertionError("expected message not received")
        msg = await ws.receive_json(timeout=left)
        if predicate(msg):
            return msg


async def join(client, role, room, password=""):
    ws = await client.ws_connect("/ws")
    await ws.send_json({"t": "hello", "room": room, "role": role, "password": password})
    return ws, await ws.receive_json(timeout=4)


class ServerIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = TestServer(create_app("classroom-password"))
        self.client = TestClient(self.server)
        await self.client.start_server()
        self.sockets = []

    async def asyncTearDown(self):
        await asyncio.gather(*(ws.close() for ws in self.sockets), return_exceptions=True)
        await self.client.close()

    def track(self, ws):
        self.sockets.append(ws)
        return ws

    async def test_http_home_and_healthcheck(self):
        async with self.client.get("/") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("makeWebSocketNet", await resp.text())
        async with self.client.get("/healthz") as resp:
            self.assertEqual((await resp.json())["status"], "ok")

    async def test_70_all_see_each_other_and_server_judges(self):
        teacher, ready = await join(self.client, "teacher", "9876", "classroom-password")
        self.track(teacher)
        self.assertEqual(ready["t"], "welcome")
        ids, students = [], []
        for i in range(MAX_STUDENTS):
            student, r = await join(self.client, "student", "9876")
            self.track(student)
            self.assertEqual(r["t"], "welcome")
            if i == MAX_STUDENTS - 1:
                self.assertEqual(len(r["players"]), MAX_STUDENTS - 1)
            sid = r["id"]
            self.assertNotIn(sid, ids)
            ids.append(sid)
            students.append(student)
            await student.send_json({"t": "put", "d": {"n": f"S{i:02d}", "c": i % 10, "x": 620, "y": 600}})
            await message_of(teacher, lambda m, sid=sid: m.get("t") == "player" and m.get("id") == sid)
        rejected, error = await join(self.client, "student", "9876")
        self.track(rejected)
        self.assertEqual(error["t"], "error")
        self.assertIn("70", error["message"])

        for i, student in enumerate(students):
            await student.send_json({"t": "patch", "d": {"x": 300 if i < 35 else 800, "y": 400}})
        update = await message_of(teacher, lambda m: m.get("t") == "positions" and len(m["rows"]) == 70, timeout=10)
        self.assertEqual(len(update["rows"]), 70)
        other = await message_of(students[-1], lambda m: m.get("t") == "positions" and len(m["rows"]) == 70)
        self.assertEqual({row[0] for row in other["rows"]}, set(ids))

        await teacher.send_json({"t": "command", "name": "question", "args": {"q": "지구는 둥글다", "dur": 10, "qtype": "ox"}})
        question = await message_of(teacher, lambda m: m.get("t") == "game" and m["g"]["phase"] == "question")
        self.assertEqual(question["g"]["round"], 1)
        await students[0].send_json({"t": "command", "name": "new"})
        self.assertEqual(self.client.app["rooms"]["9876"].game["phase"], "question")
        await teacher.send_json({"t": "command", "name": "stop"})
        await message_of(teacher, lambda m: m.get("t") == "game" and m["g"]["phase"] == "judging")
        locked = await message_of(teacher, lambda m: m.get("t") == "game" and m["g"]["phase"] == "locked", timeout=5)
        self.assertEqual(locked["g"]["pickN"], {"O": 35, "X": 35})
        await students[0].send_json({"t": "patch", "d": {"x": 800, "y": 400}})
        await asyncio.sleep(.05)
        self.assertEqual(self.client.app["rooms"]["9876"].players[ids[0]]["x"], 300)
        await teacher.send_json({"t": "command", "name": "reveal", "args": {"answer": "O", "revive": False}})
        revealed = await message_of(teacher, lambda m: m.get("t") == "game" and m["g"]["phase"] == "reveal")
        self.assertEqual(revealed["g"]["survivors"], 35)
        self.assertEqual(revealed["g"]["outCount"], 35)
        await teacher.send_json({"t": "command", "name": "close"})
        await message_of(teacher, lambda m: m.get("t") == "game" and bool(m["g"]["closed"]))

    async def test_brief_wifi_disconnect_preserves_student_identity_and_state(self):
        teacher, _ = await join(self.client, "teacher", "2468", "classroom-password")
        self.track(teacher)
        student, welcome = await join(self.client, "student", "2468")
        self.track(student)
        token, pid = welcome["session"], welcome["id"]
        await student.send_json({"t": "put", "d": {"n": "연결복원", "c": 2, "x": 800, "y": 410}})
        await message_of(teacher, lambda m: m.get("t") == "player" and m.get("id") == pid)
        await teacher.send_json({"t": "command", "name": "question", "args": {"q": "시험", "dur": 10, "qtype": "ox"}})
        await message_of(teacher, lambda m: m.get("t") == "game" and m["g"]["phase"] == "question")
        await student.close()
        await asyncio.sleep(.03)
        self.assertIn(pid, self.client.app["rooms"]["2468"].players)
        reopened = await self.client.ws_connect("/ws")
        self.track(reopened)
        await reopened.send_json({"t": "hello", "room": "2468", "role": "student", "session": token})
        resumed = await reopened.receive_json(timeout=4)
        self.assertEqual(resumed["id"], pid)
        self.assertEqual(resumed["g"]["phase"], "question")

    async def test_teacher_password_and_new_join_during_game(self):
        bad, resp = await join(self.client, "teacher", "1357", "wrong-password")
        self.track(bad)
        self.assertEqual(resp["t"], "error")
        teacher, resp = await join(self.client, "teacher", "1357", "classroom-password")
        self.track(teacher)
        self.assertEqual(resp["t"], "welcome")
        await teacher.send_json({"t": "command", "name": "question", "args": {"q": "test", "dur": 10, "qtype": "mc", "opts": ["a","b","c","d"]}})
        await message_of(teacher, lambda m: m.get("t") == "game" and m["g"]["phase"] == "question")
        late, resp = await join(self.client, "student", "1357")
        self.track(late)
        sid = resp["id"]
        await late.send_json({"t": "put", "d": {"n": "late", "c": 4, "x": 300, "y": 400}})
        await message_of(teacher, lambda m: m.get("t") == "player" and m.get("id") == sid)
        g = self.client.app["rooms"]["1357"].game
        self.assertEqual(g["late"][sid], 1)
        self.assertEqual(g["out"][sid], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
