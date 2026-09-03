#!/usr/bin/env python3
# Author: joelsnl and Anthropic Claude
"""
Build script for creating a standalone executable using PyInstaller.

Regenerates HuaEPUB.spec each run (deletes any leftover spec first).
Do not commit the generated spec — especially not a stale CustomTkinter one.
"""
import importlib.util
import sys
import subprocess
import shutil
from pathlib import Path

from core.branding import EXE_BASENAME

# Keep in sync with requirements-dev.txt and .github/workflows/release.yml
_PINNED_PYINSTALLER = "pyinstaller==6.22.2"


def build():
    """Build the application using PyInstaller."""
    
    # Get the directory of this script
    script_dir = Path(__file__).parent.absolute()
    
    # Determine OS-specific settings
    separator = ';' if sys.platform == 'win32' else ':'
    exe_name = f"{EXE_BASENAME}.exe" if sys.platform == 'win32' else EXE_BASENAME
    
    if importlib.util.find_spec("PyInstaller") is None:
        print(f"Installing {_PINNED_PYINSTALLER}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", _PINNED_PYINSTALLER])
    
    # Clean previous builds
    for folder in ['build', 'dist']:
        folder_path = script_dir / folder
        if folder_path.exists():
            print(f"Cleaning {folder}...")
            shutil.rmtree(folder_path)
    
    spec_file = script_dir / f"{EXE_BASENAME}.spec"
    if spec_file.exists():
        spec_file.unlink()
    # Remove legacy spec if present
    legacy_spec = script_dir / "NovelDownloader.spec"
    if legacy_spec.exists():
        legacy_spec.unlink()
    
    # PyInstaller arguments
    args = [
        'pyinstaller',
        f'--name={EXE_BASENAME}',
        '--onefile',                    # Single executable
        '--windowed',                   # No console window
        '--noconfirm',                  # Overwrite without asking
        f'--distpath={script_dir / "dist"}',
        f'--workpath={script_dir / "build"}',
        f'--specpath={script_dir}',
        
        # Hidden imports (modules that PyInstaller might miss)
        '--hidden-import=requests',
        '--hidden-import=lxml',
        '--hidden-import=lxml.html',
        '--hidden-import=lxml.etree',
        '--hidden-import=bs4',
        '--hidden-import=ebooklib',
        '--hidden-import=ebooklib.epub',
        '--hidden-import=PIL',
        '--hidden-import=PySide6',
        '--hidden-import=gui',
        '--hidden-import=gui.main_window',
        '--hidden-import=gui.app',
        '--hidden-import=httpx',
        '--hidden-import=core.ollama_setup',
        '--hidden-import=core.reader',
        '--hidden-import=core.reading',
        '--hidden-import=parsers.pagination',
        '--hidden-import=gui.pages.reader_page',
        '--hidden-import=gui.workers.reader_worker',
        '--hidden-import=gui.window',
        '--hidden-import=gui.window.worker_host',
        '--hidden-import=gui.window.reader_actions',
        '--hidden-import=gui.window.drive_actions',
        '--hidden-import=gui.window.library_actions',
        '--hidden-import=core.atomic_io',
        '--hidden-import=core.ad_detect',
        '--hidden-import=core.gtx_throttle',
        '--hidden-import=core.polish',
        '--hidden-import=core.polish.api',
        '--hidden-import=core.polish.detect',
        '--hidden-import=core.polish.engine',
        '--hidden-import=core.polish.glossary',
        '--hidden-import=core.polish.hardware',
        '--hidden-import=core.polish.paths',
        '--hidden-import=core.polish.prompts',
        '--hidden-import=core.polish.qwen_tokens',
        '--hidden-import=core.polish.rewrite',
        '--hidden-import=core.polish.router',
        '--hidden-import=core.polish.serve',
        '--hidden-import=core.polish.spans',
        '--hidden-import=core.polish.tagger',
        '--hidden-import=core.polish.tagger_train',
        '--hidden-import=core.translation',
        '--hidden-import=core.translation.glossary',
        '--hidden-import=core.translation.harvest',
        '--hidden-import=core.translation.qwen_glossary',
        '--hidden-import=pypinyin',
        '--hidden-import=core.translation.nmt',
        '--hidden-import=core.translation.novel_translator',
        '--hidden-import=core.translation.pack',
        
        # Collect Qt platform plugins / resources
        '--collect-all=PySide6',
        
        # Add packages as data (in case of import issues)
        f'--add-data={script_dir / "core"}{separator}core',
        f'--add-data={script_dir / "parsers"}{separator}parsers',
        f'--add-data={script_dir / "gui"}{separator}gui',
        
        # Main script
        str(script_dir / 'app.py'),
    ]
    
    print("Building with PyInstaller...")
    print(f"Command: {' '.join(args)}")
    print()
    
    # Run PyInstaller
    result = subprocess.run(args, cwd=script_dir)
    
    if result.returncode == 0:
        exe_path = script_dir / "dist" / exe_name
        if exe_path.exists():
            size_mb = exe_path.stat().st_size / (1024 * 1024)
            print()
            print("=" * 50)
            print("Build successful!")
            print(f"Executable: {exe_path}")
            print(f"Size: {size_mb:.1f} MB")
            print("=" * 50)
        else:
            print("Build completed but executable not found.")
    else:
        print("Build failed!")
        sys.exit(1)

if __name__ == "__main__":
    build()
