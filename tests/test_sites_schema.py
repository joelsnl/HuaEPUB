"""Schema-validate parsers/sites.json (required keys, no duplicate domains)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Set

SITES_PATH = Path(__file__).resolve().parents[1] / "parsers" / "sites.json"

# Keys SiteConfigParser / GenericParser actually read, plus documented extras.
ALLOWED_KEYS = frozenset({
    "name",
    "domains",
    "content",
    "title",
    "author",
    "author_index",
    "description",
    "cover",
    "cover_template",
    "chapter_title",
    "chapter_list",
    "chapter_list_url",
    "chapter_href_contains",
    "remove",
    "reverse",
    "toc_link",
    "delay",
    "encoding",
    "language",
    "tags",
    "book_id",
    "referer",
    "origin",
    "headers",
    "visit_toc_first",
})


def _load_sites() -> List[dict]:
    data = json.loads(SITES_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, list), "sites.json must be a JSON array"
    return data


def _nonempty_strings(value: Any, *, field: str, name: str) -> List[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        raise AssertionError(f"{name}: {field} must be a string or list of strings")
    out = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise AssertionError(f"{name}: {field} has an empty or non-string entry")
        out.append(item.strip())
    if not out:
        raise AssertionError(f"{name}: {field} is empty")
    return out


def _check_spec(spec: Any, index: int) -> str:
    assert isinstance(spec, dict), f"entry {index} is not an object"
    name = spec.get("name")
    assert isinstance(name, str) and name.strip(), f"entry {index} missing name"
    name = name.strip()

    unknown = set(spec) - ALLOWED_KEYS
    assert not unknown, f"{name}: unknown keys {sorted(unknown)}"

    domains = spec.get("domains")
    assert isinstance(domains, list) and domains, f"{name}: domains must be a non-empty list"
    for domain in domains:
        assert isinstance(domain, str) and domain.strip(), f"{name}: empty domain"
        assert " " not in domain.strip(), f"{name}: domain has whitespace: {domain!r}"
        assert "/" not in domain and ":" not in domain, f"{name}: domain looks like a URL: {domain!r}"

    _nonempty_strings(spec.get("content"), field="content", name=name)

    if "chapter_list" in spec:
        _nonempty_strings(spec["chapter_list"], field="chapter_list", name=name)
    if "delay" in spec:
        assert isinstance(spec["delay"], (int, float)), f"{name}: delay must be a number"
    if "reverse" in spec:
        assert isinstance(spec["reverse"], bool), f"{name}: reverse must be a boolean"
    if "visit_toc_first" in spec:
        assert isinstance(spec["visit_toc_first"], bool), f"{name}: visit_toc_first must be a boolean"
    if "author_index" in spec:
        assert isinstance(spec["author_index"], int), f"{name}: author_index must be an int"
    if "headers" in spec:
        assert isinstance(spec["headers"], dict), f"{name}: headers must be an object"
    return name


class TestSitesSchema:
    def test_every_entry_is_valid(self):
        sites = _load_sites()
        assert len(sites) >= 300
        seen_domains: Dict[str, str] = {}
        seen_names: Set[str] = set()
        for i, spec in enumerate(sites):
            name = _check_spec(spec, i)
            seen_names.add(name)
            for domain in spec["domains"]:
                key = domain.strip().lower()
                prev = seen_domains.get(key)
                assert prev is None, f"duplicate domain {key!r} ({prev} and {name})"
                seen_domains[key] = name
        assert len(seen_domains) >= len(sites)

    def test_known_hosts_present(self):
        sites = _load_sites()
        names = {spec["name"] for spec in sites}
        for required in ("twkan.com", "69shuba.com", "uukanshu.cc"):
            assert required in names
