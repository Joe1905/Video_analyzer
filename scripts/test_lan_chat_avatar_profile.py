import base64
import tempfile
import unittest
from pathlib import Path

from lan_chat import LanChatError, LanChatStore


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class LanChatAvatarProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.store = LanChatStore(root / "lan-chat.sqlite")
        self.store.initialize()
        self.token = "avatar-profile-test-token"
        self.user, _ = self.store.register(self.token, "头像测试")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_profile_can_save_edited_png_avatar(self) -> None:
        data_url = "data:image/png;base64," + base64.b64encode(PNG_1X1).decode("ascii")

        user = self.store.update_profile(self.token, "新头像", data_url)
        payload, content_type = self.store.avatar_bytes(user["id"])

        self.assertEqual("新头像", user["nickname"])
        self.assertEqual("ready", user["avatarStatus"])
        self.assertEqual("image/png", content_type)
        self.assertEqual(PNG_1X1, payload)

    def test_profile_rejects_non_png_avatar_payload(self) -> None:
        data_url = "data:image/jpeg;base64," + base64.b64encode(b"not-an-image").decode("ascii")

        with self.assertRaises(LanChatError):
            self.store.update_profile(self.token, "头像测试", data_url)

    def test_avatar_ui_contains_picker_editor_and_ten_defaults(self) -> None:
        html = (Path(__file__).parent / "static" / "lan_chat.html").read_text(encoding="utf-8")

        self.assertIn('id="avatarPickerModal"', html)
        self.assertIn('id="avatarEditorModal"', html)
        self.assertIn('id="avatarCropCanvas"', html)
        self.assertIn('id="avatarStrengthRange"', html)
        self.assertIn("Array.from({length:10}", html)
        for index in range(1, 11):
            avatar = Path(__file__).parent / "static" / "assets" / "default-avatars" / f"{index:02d}.png"
            self.assertTrue(avatar.is_file(), avatar)

    def test_profile_route_accepts_avatar_sized_json_requests(self) -> None:
        source = (Path(__file__).parent / "web_app.py").read_text(encoding="utf-8")

        self.assertIn("PROFILE_AVATAR_MAX_BYTES", source)
        self.assertIn('is_profile_request = path == "/api/lan-chat/profile"', source)
        self.assertIn(
            "json_max_bytes = (PROFILE_AVATAR_MAX_BYTES * 4 // 3) + 256 * 1024",
            source,
        )


if __name__ == "__main__":
    unittest.main()
