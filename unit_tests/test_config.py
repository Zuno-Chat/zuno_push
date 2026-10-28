from twisted.trial import unittest

from zuno_push.config import DEFAULT_FCM_APP_IDS, DEFAULT_VOIP_APP_IDS, parse_config

URL = "http://127.0.0.1:5000/_matrix/push/v1/notify"


class ParseConfigTests(unittest.TestCase):
    def test_minimal_config_uses_contract_defaults(self) -> None:
        config = parse_config({"sygnal_notify_url": URL})
        self.assertIsNone(config.error)
        self.assertEqual(config.sygnal_notify_url, URL)
        self.assertEqual(config.voip_app_ids, DEFAULT_VOIP_APP_IDS)
        self.assertEqual(config.fcm_app_ids, DEFAULT_FCM_APP_IDS)
        self.assertEqual(config.max_ring_targets, 5000)
        self.assertEqual(config.disabled_features, frozenset())

    def test_full_config(self) -> None:
        config = parse_config(
            {
                "sygnal_notify_url": URL,
                "voip_app_ids": ["im.zuno.chat.ios.voip"],
                "fcm_app_ids": [],
                "max_ring_targets": 100,
                "disabled_features": ["catchup"],
            }
        )
        self.assertIsNone(config.error)
        self.assertEqual(config.voip_app_ids, ("im.zuno.chat.ios.voip",))
        self.assertEqual(config.fcm_app_ids, ())
        self.assertEqual(config.disabled_features, frozenset({"catchup"}))

    def test_bad_values_never_raise(self) -> None:
        for raw in (
            None,
            "nope",
            {},
            {"sygnal_notify_url": "http://sygnal/notify"},
            {"sygnal_notify_url": URL, "max_ring_targets": 0},
            {"sygnal_notify_url": URL, "max_ring_targets": True},
            {"sygnal_notify_url": URL, "disabled_features": ["everything"]},
            {"sygnal_notify_url": URL, "typo_key": 1},
            {"sygnal_notify_url": URL, "voip_app_ids": []},
            {"sygnal_notify_url": URL, "voip_app_ids": [1]},
            {"sygnal_notify_url": URL, "fcm_app_ids": "im.zuno.chat.android"},
            {"sygnal_notify_url": URL, "fcm_app_ids": [""]},
            {"sygnal_notify_url": URL, "fcm_app_ids": ["im.zuno.chat.ios.voip"]},
        ):
            config = parse_config(raw)
            self.assertIsNotNone(config.error, raw)
