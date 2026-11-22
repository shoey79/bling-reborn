# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: builds the tray app (BlinkSync.exe, no console) and the command-line tool
# (blink-sync-cli.exe) into one folder that shares a single copy of Python and the libraries.
# Run via build.ps1, which sets BLINKSYNC_VERSION and creates build\app.ico first.

import os
import re
import sys

version = os.environ.get("BLINKSYNC_VERSION", "1.0.0")
icon = os.path.join("build", "app.ico")
hidden = ["pystray._win32", "PIL._tkinter_finder"] if sys.platform == "win32" else ["PIL._tkinter_finder"]


def version_info(description, filename):
    if sys.platform != "win32":
        return None
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo)
    nums = [int(n) for n in re.findall(r"\d+", version)][:4]
    nums += [0] * (4 - len(nums))
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=tuple(nums), prodvers=tuple(nums)),
        kids=[
            StringFileInfo([StringTable("080904B0", [
                StringStruct("FileDescription", description),
                StringStruct("FileVersion", version),
                StringStruct("ProductName", "Blink Sync"),
                StringStruct("ProductVersion", version),
                StringStruct("InternalName", filename),
                StringStruct("OriginalFilename", filename),
            ])]),
            VarFileInfo([VarStruct("Translation", [2057, 1200])]),
        ])


tray = Analysis(["blink_sync_tray.py"], pathex=["."], hiddenimports=hidden)
cli = Analysis(["blink_sync.py"], pathex=["."])

tray_exe = EXE(
    PYZ(tray.pure), tray.scripts, [],
    exclude_binaries=True, name="BlinkSync", console=False, upx=False,
    icon=icon if os.path.exists(icon) else None,
    version=version_info("Blink Sync", "BlinkSync.exe"))

cli_exe = EXE(
    PYZ(cli.pure), cli.scripts, [],
    exclude_binaries=True, name="blink-sync-cli", console=True, upx=False,
    icon=icon if os.path.exists(icon) else None,
    version=version_info("Blink Sync command line", "blink-sync-cli.exe"))

COLLECT(
    tray_exe, tray.binaries, tray.datas,
    cli_exe, cli.binaries, cli.datas,
    upx=False, name="BlinkSync")
