import unittest

from bot.utils import normalize_target


class NormalizeTargetTest(unittest.TestCase):
    def test_username_variants(self) -> None:
        inputs = [
            "https://t.me/itzdhruv",
            "t.me/itzdhruv",
            "@itzdhruv",
            "itzdhruv",
        ]

        for text in inputs:
            with self.subTest(text=text):
                normalized, details = normalize_target(text)
                self.assertEqual(normalized, "itzdhruv")
                self.assertIn(details.get("type"), {"username", "public_message", "numeric_id"})


if __name__ == "__main__":
    unittest.main()
