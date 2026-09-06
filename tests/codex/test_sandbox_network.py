from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[2]


class SandboxNetworkProfileTest(unittest.TestCase):
    # Break caught: the Codex sandbox disabling network for tool commands again,
    # which makes every broker client (family, GitHub, egress proxy) fail with
    # EPERM inside the runtime container.
    def test_workspace_write_sandbox_keeps_container_network(self) -> None:
        config_path = ROOT / "profiles" / "codex" / "config.toml"
        with config_path.open("rb") as stream:
            config = tomllib.load(stream)

        self.assertIs(config["sandbox_workspace_write"]["network_access"], True)
        self.assertNotIn("sandbox_mode", config)
        self.assertNotIn("network_proxy", config.get("features", {}))
        self.assertNotIn("default_permissions", config)
        self.assertNotIn("permissions", config)


if __name__ == "__main__":
    unittest.main()
