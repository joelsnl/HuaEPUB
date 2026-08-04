# Author: joelsnl and Anthropic Claude
"""Desktop notifications when a download batch finishes."""

from __future__ import annotations

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


def _notify_windows(title: str, message: str):
    # AppUserModelID helps Windows group toasts under a stable app name
    script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$template = @"
<toast>
  <visual>
    <binding template="ToastGeneric">
      <text>{_xml_escape(title)}</text>
      <text>{_xml_escape(message)}</text>
    </binding>
  </visual>
</toast>
"@
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($template)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("{APP_NAME}").Show($toast)
"""
    subprocess.Popen(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
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
