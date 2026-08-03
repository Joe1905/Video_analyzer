import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from chat_session import ChatStore, Message, Session, save_sessions_to_disk


class ChatSessionPersistenceTests(unittest.TestCase):
    def make_store(self, root: Path) -> ChatStore:
        store = ChatStore(root / "chat_sessions.json")
        store.sessions["amazon__history"] = Session(
            id="amazon__history",
            title="历史对话",
            messages=[Message(id="message-1", role="user", content="保留历史记录")],
        )
        return store

    def test_failed_save_keeps_previous_file_intact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            save_sessions_to_disk(store)
            previous = store.sessions_file.read_bytes()

            def fail_dump(payload, file_obj, **kwargs):
                file_obj.write("[")
                raise RuntimeError("simulated interrupted write")

            with mock.patch("chat_session.json.dump", side_effect=fail_dump):
                with self.assertRaisesRegex(RuntimeError, "simulated interrupted write"):
                    save_sessions_to_disk(store)

            self.assertEqual(previous, store.sessions_file.read_bytes())
            self.assertEqual([], list(root.glob(".chat_sessions.json.*.tmp")))

    def test_concurrent_saves_are_serialized_and_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            original_dump = json.dump
            active = 0
            max_active = 0
            counter_lock = threading.Lock()

            def slow_dump(payload, file_obj, **kwargs):
                nonlocal active, max_active
                with counter_lock:
                    active += 1
                    max_active = max(max_active, active)
                try:
                    time.sleep(0.05)
                    return original_dump(payload, file_obj, **kwargs)
                finally:
                    with counter_lock:
                        active -= 1

            with mock.patch("chat_session.json.dump", side_effect=slow_dump):
                threads = [
                    threading.Thread(target=save_sessions_to_disk, args=(store,))
                    for _ in range(3)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            self.assertEqual(1, max_active)
            saved = json.loads(store.sessions_file.read_text(encoding="utf-8"))
            self.assertEqual(["amazon__history"], [item["id"] for item in saved])
            self.assertEqual([], list(root.glob(".chat_sessions.json.*.tmp")))


if __name__ == "__main__":
    unittest.main()
