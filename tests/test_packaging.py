"""The install entry point and Linux package files agree with the app."""

from pathlib import Path

from core.updater import __version__
from gui.app import run
from parsers.config import load_sites

ROOT = Path(__file__).resolve().parents[1]


def test_console_script_calls_run():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'huaepub = "gui.app:run"' in project
    assert callable(run)


def test_sites_json_loads():
    sites = load_sites()
    assert isinstance(sites, list) and sites


def test_package_versions_match_app():
    """Humans edit VERSION once. Packaging reads that file instead of a copy."""
    recorded = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert __version__ == recorded
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = {file = "VERSION"}' in project
    assert f'version = "{recorded}"' not in project
    snap = (ROOT / "snapcraft.yaml").read_text(encoding="utf-8")
    assert "adopt-info: huaepub" in snap
    assert f"version: '{recorded}'" not in snap
    assert "CRAFT_PART_SRC/VERSION" in snap
    meta = (ROOT / "packaging" / "org.joelsnl.HuaEPUB.metainfo.xml").read_text(encoding="utf-8")
    assert 'version="@VERSION@"' in meta
    assert f'version="{recorded}"' not in meta
    flatpak = (ROOT / "packaging" / "org.joelsnl.HuaEPUB.yml").read_text(encoding="utf-8")
    appimage = (ROOT / "packaging" / "build-appimage.sh").read_text(encoding="utf-8")
    assert "@VERSION@" in flatpak and "VERSION" in flatpak
    assert "@VERSION@" in appimage and "VERSION" in appimage


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
