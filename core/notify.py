# Author: joelsnl and Anthropic Claude
"""Desktop notifications when a download or app update finishes."""

from __future__ import annotations

import base64
import subprocess
import sys

from core.branding import APP_NAME, APP_TITLE


def notify(title: str, message: str):
    """
    Show a desktop notification. Best-effort; never raises.
    Windows: PowerShell toast. macOS: osascript. Linux: notify-send.
    """
    title = (title or APP_TITLE).strip()
    message = (message or "").strip()
    if not message:
        return

    try:
        if sys.platform == "win32":
            _notify_windows(title, message)
        elif sys.platform == "darwin":
            _notify_macos(title, message)
        else:
            _notify_linux(title, message)
    except Exception:
        pass


def _b64(text: str) -> str:
    """ASCII base64 — safe to embed in single-quoted PowerShell strings."""
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _notify_windows(title: str, message: str):
    """
    Toast via PowerShell. Untrusted title/message (novel names) must never be
    interpolated into expandable @\"…\"@ / double-quoted -Command text.
    Pass payloads as base64 and build XML inside PowerShell.
    """
    # APP_NAME is a trusted constant; still base64 so the script has no f-string sinks.
    script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime -ErrorAction SilentlyContinue | Out-Null
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
function Escape-Xml([string]$s) {{
  if ($null -eq $s) {{ return '' }}
  return ($s -replace '&','&amp;' -replace '<','&lt;' -replace '>','&gt;' -replace '"','&quot;' -replace "'",'&apos;')
}}
$title = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{_b64(title)}'))
$message = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{_b64(message)}'))
$appId = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{_b64(APP_NAME)}'))
$template = @'
<toast>
  <visual>
    <binding template="ToastGeneric">
      <text>__TITLE__</text>
      <text>__MESSAGE__</text>
    </binding>
  </visual>
</toast>
'@
$template = $template.Replace('__TITLE__', (Escape-Xml $title)).Replace('__MESSAGE__', (Escape-Xml $message))
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($template)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""
    subprocess.Popen(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _notify_macos(title: str, message: str):
    script = f'display notification "{_osa_escape(message)}" with title "{_osa_escape(title)}"'
    subprocess.Popen(
        ["osascript", "-e", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _osa_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _notify_linux(title: str, message: str):
    subprocess.Popen(
        ["notify-send", title, message],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
