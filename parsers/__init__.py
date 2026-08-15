# Author: joelsnl and Anthropic Claude
"""
Site parsers for HuaEPUB
Import this module to register all parsers
"""

# Import all parsers to register them.
# ORDER MATTERS: the registry is searched in import order. Dedicated parsers
# first, then the WebToEpub selector pack, then GenericParser last (it matches
# ANY http(s) URL).
from parsers.twkan import TwkanParser
from parsers.shuba69 import Shuba69Parser
from parsers.uukanshu import UUKanshuParser
from parsers.selector import SelectorParser, register_webtoepub_parsers

register_webtoepub_parsers()

from parsers.generic import GenericParser
from core.parser import register_parser

register_parser(GenericParser)

__all__ = [
    'TwkanParser',
    'Shuba69Parser',
    'UUKanshuParser',
    'SelectorParser',
    'GenericParser',
]
