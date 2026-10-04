# Author: joelsnl and Anthropic Claude
"""HuaEPUB Simple: a localhost web companion for the download-translate-EPUB pipeline.

Run from a checkout with ``python3 -m web``. Imported only by the web process;
``core/`` and ``gui/`` never import this package, and this package never
imports ``gui/`` (Qt must not load here).
"""
