# Author: joelsnl and Anthropic Claude
"""HuaEPUB server mode: the full app served to a browser from the desktop process.

``web.host.ServerHost`` runs FastAPI + uvicorn on a daemon thread inside the
desktop app (File → Server mode… or the SERVE chip). ``core/`` and ``parsers/``
never import this package, and this package never imports ``gui/`` (Qt must
not load here).
"""
