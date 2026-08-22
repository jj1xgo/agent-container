from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[2]


class StatusLineProfileTest(unittest.TestCase):
    def test_statusline_uses_verified_items_in_operational_order(self) -> None:
        config_path = ROOT / "profiles" / "codex" / "config.toml"
        with config_path.open("rb") as stream:
            config = tomllib.load(stream)

        self.assertEqual(config["cli_auth_credentials_store"], "file")
        self.assertEqual(config["forced_login_method"], "chatgpt")
        self.assertEqual(
            config["tui"]["status_line"],
            [
                "model-with-reasoning",
                "context-remaining",
                "five-hour-limit",
                "weekly-limit",
                "used-tokens",
                "git-branch",
                "project-name",
            ],
        )


if __name__ == "__main__":
    unittest.main()
