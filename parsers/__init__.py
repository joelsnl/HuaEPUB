# Author: joelsnl and Anthropic Claude
"""
Site parsers for HuaEPUB.
Import this module to register parsers.

ORDER MATTERS: SiteConfigParser (parsers/sites.json) first, GenericParser last
(it matches any http(s) URL).
"""

from parsers.config import SiteConfigParser
from parsers.generic import GenericParser
from core.parser import register_parser

register_parser(SiteConfigParser)
register_parser(GenericParser)

__all__ = [
    "SiteConfigParser",
    "GenericParser",
]
