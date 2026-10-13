import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")

import handlers.start as start  # noqa: E402
import database  # noqa: E402


class DemoVideoSpoilerTests(unittest.TestCase):
    def test_sends_videos_with_spoilers_in_saved_order(self):
        calls = []

        async def send_video(**kwargs):
            calls.append(("video", kwargs))
            return SimpleNamespace(message_id=len(calls) + 100)

        async def copy_message(**kwargs):
            calls.append(("copy", kwargs))
            return SimpleNamespace(message_id=len(calls) + 100)

        bot = SimpleNamespace(send_video=send_video, copy_message=copy_message)
        media = [
            {"message_id": 11, "type": "video", "file_id": "video-a"},
            {"message_id": 12, "type": "copy"},
            {"message_id": 13, "type": "video", "file_id": "video-b"},
        ]

        async def deliver():
            with patch.object(start.asyncio, "sleep", new=AsyncMock()):
                return await start._send_demo_messages(bot, 7, "-1001", [11, 12, 13], media)

        sent_ids = asyncio.run(deliver())

        self.assertEqual([kind for kind, _ in calls], ["video", "copy", "video"])
        self.assertEqual([call[1].get("video") for call in calls if call[0] == "video"], ["video-a", "video-b"])
        self.assertTrue(all(call[1]["has_spoiler"] is True for call in calls if call[0] == "video"))
        self.assertEqual([call[1].get("message_id") for call in calls if call[0] == "copy"], [12])
        self.assertEqual(sent_ids, [101, 102, 103])

    def test_refuses_out_of_order_metadata_without_sending(self):
        bot = SimpleNamespace(send_video=AsyncMock(), copy_message=AsyncMock())
        media = [{"message_id": 12, "type": "video", "file_id": "video-a"}]

        sent_ids = asyncio.run(start._send_demo_messages(bot, 7, "-1001", [11], media))

        self.assertEqual(sent_ids, [])
        bot.send_video.assert_not_awaited()
        bot.copy_message.assert_not_awaited()

    def test_legacy_plan_ids_are_not_copied_without_file_ids(self):
        bot = SimpleNamespace(send_video=AsyncMock(), copy_message=AsyncMock())

        async def run():
            with patch.object(start, "create_demo_session", new=AsyncMock()) as create_session:
                result = await start.send_demo_videos(
                    bot,
                    7,
                    7,
                    {"id": 1, "source_channel_id": "-1001", "demo_message_ids": [11]},
                )
            return result, create_session

        result, create_session = asyncio.run(run())

        self.assertIsNone(result)
        create_session.assert_not_awaited()
        bot.send_video.assert_not_awaited()
        bot.copy_message.assert_not_awaited()

    def test_start_and_plan_flows_use_spoiler_enabled_video_sends(self):
        bot = SimpleNamespace(send_video=AsyncMock(return_value=SimpleNamespace(message_id=101)))
        media = [{"message_id": 11, "type": "video", "file_id": "video-a"}]

        async def run():
            with (
                patch.object(start, "get_start_demo", new=AsyncMock(return_value={
                    "enabled": True,
                    "ids": [11],
                    "source": "-1001",
                    "media": media,
                })),
                patch.object(start, "create_demo_session", new=AsyncMock(return_value="start-session")),
                patch.object(start, "activate_demo_session", new=AsyncMock()) as activate,
                patch.object(start.asyncio, "sleep", new=AsyncMock()),
            ):
                await start.send_start_demo_videos(bot, 7, 7)

            with (
                patch.object(start, "create_demo_session", new=AsyncMock(return_value="plan-session")),
                patch.object(start.asyncio, "sleep", new=AsyncMock()),
            ):
                result = await start.send_demo_videos(
                    bot,
                    7,
                    7,
                    {
                        "id": 1,
                        "source_channel_id": "-1001",
                        "demo_message_ids": [11],
                        "demo_media": media,
                    },
                )
            return activate, result

        activate, result = asyncio.run(run())

        self.assertEqual(bot.send_video.await_count, 2)
        self.assertTrue(all(call.kwargs["has_spoiler"] is True for call in bot.send_video.await_args_list))
        self.assertEqual(result, ("plan-session", [101]))
        activate.assert_awaited_once()

    def test_failed_start_demo_stage_preserves_active_and_legacy_records(self):
        settings = {
            "start_demo_ids": "[5]",
            "start_demo_source": "-100-old",
            "start_demo_media": '{"ids":[5],"source":"-100-old","items":[]}',
        }

        async def get_setting(key, default=""):
            if key == "start_demo_media_pending":
                return "corrupted-pending-value"
            return settings.get(key, default)

        async def set_setting(key, value):
            settings[key] = value

        async def save():
            with (
                patch.object(database, "get_setting", new=get_setting),
                patch.object(database, "set_setting", new=set_setting),
            ):
                await database.set_start_demo_ids(
                    [11],
                    "-100-new",
                    [{"message_id": 11, "type": "video", "file_id": "video-a"}],
                )

        with self.assertRaises(RuntimeError):
            asyncio.run(save())

        self.assertEqual(settings["start_demo_ids"], "[5]")
        self.assertEqual(settings["start_demo_source"], "-100-old")
        self.assertEqual(
            settings["start_demo_media"],
            '{"ids":[5],"source":"-100-old","items":[]}',
        )


if __name__ == "__main__":
    unittest.main()