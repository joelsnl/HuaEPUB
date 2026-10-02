"""The install entry point and Linux package files agree with the app."""

from pathlib import Path

from core.updater import __version__
from gui.app import run
from parsers.config import load_sites

ROOT = Path(__file__).resolve().parents[1]


def _version_line(text: str, prefix: str) -> str:
    for line in text.splitlines():
        if line.startswith(prefix):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise AssertionError(prefix)


def test_console_script_calls_run():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'huaepub = "gui.app:run"' in project
    assert callable(run)


def test_sites_json_loads():
    sites = load_sites()
    assert isinstance(sites, list) and sites


def test_package_versions_match_app():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert _version_line(project, "version = ") == __version__
    snap = (ROOT / "snapcraft.yaml").read_text(encoding="utf-8")
    assert f"version: '{__version__}'" in snap
    meta = (ROOT / "packaging" / "org.joelsnl.HuaEPUB.metainfo.xml").read_text(encoding="utf-8")
    assert f'version="{__version__}"' in meta


def test_linux_launchers_name_the_real_command():
    desktop = (ROOT / "packaging" / "org.joelsnl.HuaEPUB.desktop").read_text(encoding="utf-8")
    assert "Exec=huaepub %U" in desktop
    assert "MimeType" not in desktop
    manifest = (ROOT / "packaging" / "org.joelsnl.HuaEPUB.yml").read_text(encoding="utf-8")
    assert "command: huaepub" in manifest
    assert "path: .." in manifest
    assert "--share=network" in manifest
    assert "done-callbacks" not in manifest
    assert "--filesystem=host" not in manifest
    snap = (ROOT / "snapcraft.yaml").read_text(encoding="utf-8")
    assert "confinement: strict" in snap
    assert "dconfinement" not in snap
    script = (ROOT / "packaging" / "build-appimage.sh").read_text(encoding="utf-8")
    assert "Exec=HuaEPUB %U" in script
    assert "appimagetool-x86_64.AppImage" not in script
