# Author: joelsnl and Anthropic Claude
"""
Site parsers for HuaEPUB
Import this module to register all parsers
"""

# Import all parsers to register them.
# ORDER MATTERS: the registry is searched in import order, and GenericParser
# matches ANY http(s) URL - it must be imported LAST so it only acts as a
# fallback for sites without a dedicated parser.
from parsers.twkan import TwkanParser
from parsers.shuba69 import Shuba69Parser
from parsers.uukanshu import UUKanshuParser
from parsers.generic import GenericParser

__all__ = ['TwkanParser', 'Shuba69Parser', 'UUKanshuParser', 'GenericParser']
