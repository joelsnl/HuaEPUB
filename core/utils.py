"""Small shared helpers used by the GUI and builders."""


def format_eta(seconds: float) -> str:
    """Format a duration like '3m 20s' or '1h 12m'."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
