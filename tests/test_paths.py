from __future__ import annotations

import unittest
from pathlib import Path

from openthesis.paths import default_data_dir


class DefaultDataDirTests(unittest.TestCase):
    def test_explicit_override_wins_on_every_platform(self) -> None:
        result = default_data_dir(
            environ={"OPENTHESIS_DATA_DIR": "D:/OpenThesisData"},
            home=Path("/Users/example"),
            platform_system="Darwin",
        )

        self.assertEqual(result, Path("D:/OpenThesisData"))

    def test_windows_uses_local_app_data(self) -> None:
        result = default_data_dir(
            environ={"LOCALAPPDATA": "D:/LocalAppData"},
            home=Path("C:/Users/example"),
            platform_system="Windows",
        )

        self.assertEqual(result, Path("D:/LocalAppData/OpenThesis"))

    def test_macos_uses_application_support(self) -> None:
        result = default_data_dir(
            environ={},
            home=Path("/Users/example"),
            platform_system="Darwin",
        )

        self.assertEqual(
            result,
            Path("/Users/example/Library/Application Support/OpenThesis"),
        )

    def test_linux_honors_xdg_data_home(self) -> None:
        result = default_data_dir(
            environ={"XDG_DATA_HOME": "/data/example"},
            home=Path("/home/example"),
            platform_system="Linux",
        )

        self.assertEqual(result, Path("/data/example/OpenThesis"))


if __name__ == "__main__":
    unittest.main()
