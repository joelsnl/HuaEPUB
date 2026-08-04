#!/usr/bin/env python3
# Author: joelsnl and Anthropic Claude
"""
Build script for creating standalone executable using PyInstaller
"""
import os
import sys
import subprocess
import shutil
from pathlib import Path

from core.branding import EXE_BASENAME


def build():
    """Build the application using PyInstaller."""
    
    # Get the directory of this script
    script_dir = Path(__file__).parent.absolute()
    
    # Determine OS-specific settings
    separator = ';' if sys.platform == 'win32' else ':'
    exe_name = f"{EXE_BASENAME}.exe" if sys.platform == 'win32' else EXE_BASENAME
    
    # Check if PyInstaller is installed
    try:
        import PyInstaller
    except ImportError:
        print("Installing PyInstaller...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])
    
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
        '--hidden-import=customtkinter',
        
        # Collect all customtkinter data
        '--collect-all=customtkinter',
        
        # Add core and parsers as data (in case of import issues)
        f'--add-data={script_dir / "core"}{separator}core',
        f'--add-data={script_dir / "parsers"}{separator}parsers',
        
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
