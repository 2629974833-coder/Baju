"""Bounded, local-only schema.org episode catalog evidence."""

import hashlib
import json
from urllib.parse import urljoin, urlsplit


def same_origin_url(value, page_url):
    try:
        if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
            return None
        value.encode("utf-8")
        resolved = urljoin(page_url, value)
        target, page = urlsplit(resolved), urlsplit(page_url)

        def origin(parsed):
            return (
                parsed.scheme,
                parsed.hostname,
                parsed.port
                if parsed.port is not None
                else (443 if parsed.scheme == "https" else 80),
            )

        if (
            target.scheme not in {"http", "https"}
            or not target.hostname
            or target.username
            or target.password
            or origin(target) != origin(page)
        ):
            return None
        return resolved
    except (ValueError, TypeError, UnicodeError):
        return None


def _safe_text(value):
    return str(value or "")[:200].encode("utf-8", errors="replace").decode("utf-8")


def metadata_for(catalog_url, title=""):
    return {
        "seriesKey": "generic-" + hashlib.sha256(catalog_url.encode()).hexdigest(),
        "catalogUrl": catalog_url,
        "title": _safe_text(title),
    }


MAX_SCRIPTS = 32
MAX_BYTES = 512 * 1024
MAX_NODES = 5000
MAX_DEPTH = 32
MAX_REF_HOPS = 16
MAX_VISITS = 10000


class _Issue(Exception):
    pass


def _values(value):
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _field(node, name):
    values = [
        node[key]
        for key in (name, "https://schema.org/" + name, "http://schema.org/" + name)
        if key in node
    ]
    if len(values) > 1 and any(value != values[0] for value in values[1:]):
        raise _Issue("catalog-conflict")
    return values[0] if values else None


def _types(node):
    return {
        value.removeprefix("https://schema.org/").removeprefix("http://schema.org/")
        for value in _values(node.get("@type"))
        if isinstance(value, str)
    }


def _integer(value):
    if (
        isinstance(value, str)
        and len(value.strip()) <= 4
        and value.strip().isascii()
        and value.strip().isdigit()
    ):
        value = int(value.strip())
    return value if type(value) is int and 1 <= value <= 1000 else None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("Non-finite JSON number")


class _Graph:
    def __init__(self, documents, page_url, max_nodes, max_depth, max_refs, max_visits):
        self.page_url, self.max_refs = page_url, max_refs
        self.max_visits = max_visits
        self.nodes, self.index = [], {}
        self.conflicts = set()
        stack = [(document, 0) for document in documents]
        visited = 0
        while stack:
            value, depth = stack.pop()
            if not isinstance(value, (dict, list)):
                continue
            visited += 1
            if visited > max_nodes or depth > max_depth:
                raise _Issue("structured-limit")
            if isinstance(value, list):
                stack.extend((child, depth + 1) for child in value)
                continue
            self.nodes.append(value)
            stack.extend((child, depth + 1) for child in value.values())
            key = self.key(value.get("@id"))
            if key:
                existing = self.index.setdefault(key, {})
                for field, child in value.items():
                    if (
                        field in existing
                        and field not in {"@context", "@id"}
                        and existing[field] != child
                    ):
                        self.conflicts.add(key)
                        continue
                    existing[field] = child

    def key(self, value):
        if not isinstance(value, str) or not value:
            return None
        try:
            return value if value.startswith("_:") else urljoin(self.page_url, value)
        except ValueError:
            raise _Issue("structured-invalid") from None

    def resolve(self, value, *, strict=True):
        if isinstance(value, str):
            value = {"@id": value}
        if not isinstance(value, dict):
            return {}
        key = self.key(value.get("@id"))
        if strict and key in self.conflicts:
            raise _Issue("catalog-conflict")
        return self.index.get(key, value)

    def navigation(self, node):
        raw = _field(node, "url")
        declared = []
        for value in _values(raw):
            if isinstance(value, dict):
                value = value.get("@id")
            if not isinstance(value, str) or not value.strip():
                raise _Issue("structured-invalid")
            url = same_origin_url(value, self.page_url)
            if not url:
                raise _Issue("structured-invalid")
            declared.append(url)
        identifier = node.get("@id")
        id_url = None
        if (
            isinstance(identifier, str)
            and identifier.strip()
            and not identifier.startswith(("#", "_:"))
        ):
            parsed = urlsplit(identifier)
            if not parsed.fragment or parsed.fragment.startswith(("/", "!/", "?")):
                id_url = same_origin_url(identifier, self.page_url)
        if len(set(declared)) > 1 or (declared and id_url and id_url != declared[0]):
            raise _Issue("catalog-conflict")
        return declared[0] if declared else id_url


def _empty(reasons=(), limited=False):
    return {
        "links": [],
        "expectedCount": None,
        "linksTruncated": limited,
        "reasons": list(dict.fromkeys(reasons)),
        "seriesMetadata": None,
        "catalogLink": None,
        "discoverySources": [],
    }


def parse_structured_catalog(
    scripts,
    page_url,
    *,
    max_scripts=MAX_SCRIPTS,
    max_bytes=MAX_BYTES,
    max_nodes=MAX_NODES,
    max_depth=MAX_DEPTH,
    max_ref_hops=MAX_REF_HOPS,
    max_visits=MAX_VISITS,
):
    """Interpret only schema episode relationships already present in page JSON."""
    documents, reasons = [], []
    total = 0
    if len(scripts) > max_scripts:
        return _empty(["structured-limit"], True)
    for script in scripts:
        if not isinstance(script, str):
            reasons.append("structured-invalid")
            continue
        try:
            total += len(script.encode("utf-8"))
        except UnicodeError:
            reasons.append("structured-invalid")
            continue
        if total > max_bytes:
            return _empty(["structured-limit"], True)
        try:
            value = json.loads(
                script, object_pairs_hook=_unique_object, parse_constant=_reject_constant
            )
            if not isinstance(value, (dict, list)):
                raise ValueError("JSON-LD root is not an object or array")
            documents.append(value)
        except RecursionError:
            return _empty(["structured-limit"], True)
        except (ValueError, TypeError):
            reasons.append("structured-invalid")
    try:
        graph = _Graph(documents, page_url, max_nodes, max_depth, max_ref_hops, max_visits)
        return _catalog_from_graph(graph, reasons)
    except _Issue as issue:
        return _empty([*reasons, str(issue)], str(issue) == "structured-limit")


def _catalog_from_graph(graph, reasons):
    owners, aliases = {}, {}
    for raw in graph.nodes:
        node = graph.resolve(raw, strict=False)
        types = _types(node) & {"TVSeries", "TVSeason"}
        if not types:
            continue
        url = graph.navigation(node)
        if not url:
            continue
        if len(types) > 1 or (url in owners and owners[url]["type"] not in types):
            raise _Issue("catalog-conflict")
        owner = owners.setdefault(url, {"type": next(iter(types)), "nodes": []})
        if node not in owner["nodes"]:
            owner["nodes"].append(node)
        aliases[url] = url
        if graph.key(node.get("@id")):
            aliases[graph.key(node["@id"])] = url

    def parents(node, property_name, *, strict=False):
        found = set()
        for raw in _values(_field(node, property_name)):
            parent = graph.resolve(raw, strict=strict)
            url = graph.navigation(parent)
            key = graph.key(parent.get("@id"))
            found.add(aliases.get(key) or aliases.get(url) or key or url or "unresolved-parent")
        return found

    episodes = []
    for raw in graph.nodes:
        if "TVEpisode" in _types(raw) and raw not in episodes:
            episodes.append(raw)
    eligible = {
        url
        for url, owner in owners.items()
        if any(
            _field(node, "episode") is not None
            or _field(node, "hasPart") is not None
            or _field(node, "containsSeason") is not None
            for node in owner["nodes"]
        )
    }
    for node in episodes:
        eligible.update(
            (parents(node, "partOfSeason") or parents(node, "partOfSeries")) & owners.keys()
        )
    for raw in graph.nodes:
        node = graph.resolve(raw, strict=False)
        if "ItemList" in _types(node):
            eligible.update(
                (parents(node, "partOfSeason") or parents(node, "partOfSeries")) & owners.keys()
            )
    preferred = {url for url in eligible if url == graph.page_url}
    for node in episodes:
        try:
            on_page = graph.navigation(node) == graph.page_url
        except _Issue:
            on_page = False
        if on_page:
            preferred.update(
                (parents(node, "partOfSeason") or parents(node, "partOfSeries")) & eligible
            )
    for raw in graph.nodes:
        node = graph.resolve(raw, strict=False)
        if "WebPage" in _types(node) and graph.navigation(node) == graph.page_url:
            for value in _values(_field(node, "mainEntity")):
                entity = graph.resolve(value, strict=False)
                key = aliases.get(graph.key(entity.get("@id"))) or graph.navigation(entity)
                if key in eligible:
                    preferred.add(key)
    choices = preferred or eligible
    if not choices:
        return _empty(reasons)
    if len(choices) != 1:
        raise _Issue("catalog-conflict")
    selected = next(iter(choices))
    owner = owners[selected]
    owner["nodes"] = [graph.resolve(node) for node in owner["nodes"]]
    title = next(
        (_field(node, "name") for node in owner["nodes"] if isinstance(_field(node, "name"), str)),
        "",
    )
    metadata = metadata_for(selected, title)
    links, ordinals, counts = {}, {}, set()
    owner_series = set().union(
        *(parents(node, "partOfSeries", strict=True) for node in owner["nodes"])
    )
    expanded = set()
    visits = 0

    def add_counts(value):
        for count in _values(value):
            number = _integer(count)
            if number is None:
                reasons.append("structured-invalid")
            else:
                counts.add(number)

    def add_episode(node):
        node = graph.resolve(node)
        season = parents(node, "partOfSeason", strict=True)
        series = parents(node, "partOfSeries", strict=True)
        if owner["type"] == "TVSeason":
            if (season and season != {selected}) or (
                series and owner_series and series != owner_series
            ):
                raise _Issue("catalog-conflict")
        elif season or (series and series != {selected}):
            raise _Issue("catalog-conflict")
        number = _integer(_field(node, "episodeNumber"))
        try:
            url = graph.navigation(node)
        except _Issue as issue:
            if str(issue) == "catalog-conflict":
                raise
            url = None
        if number is None or not url:
            reasons.append("structured-invalid")
            return
        if (number in ordinals and ordinals[number] != url) or (
            url in links and links[url]["ordinal"] != number
        ):
            raise _Issue("catalog-conflict")
        ordinals[number] = url
        links[url] = {
            "url": url,
            "ordinal": number,
            "title": _safe_text(_field(node, "name")),
            "access": "unknown",
        }

    def visit(raw, bound, depth=0, path=frozenset()):
        nonlocal visits
        if depth > graph.max_refs:
            raise _Issue("structured-limit")
        for value in _values(raw):
            visits += 1
            if visits > graph.max_visits:
                raise _Issue("structured-limit")
            node = graph.resolve(value)
            marker = graph.key(node.get("@id")) or id(node)
            if marker in path:
                raise _Issue("structured-limit")
            expansion = (marker, bound)
            if expansion in expanded:
                continue
            node_path = path | {marker}
            types = _types(node)
            if types & {"TVSeries", "TVSeason"}:
                raise _Issue("catalog-conflict")
            if "TVEpisode" in types or (bound and _field(node, "episodeNumber") is not None):
                add_episode(node)
            elif "ItemList" in types:
                declared = parents(node, "partOfSeason", strict=True) or parents(
                    node, "partOfSeries", strict=True
                )
                if declared and declared != {selected}:
                    raise _Issue("catalog-conflict")
                if bound or declared == {selected}:
                    add_counts(_field(node, "numberOfItems"))
                    visit(_field(node, "itemListElement"), True, depth + 1, node_path)
            elif "ListItem" in types or (bound and _field(node, "item") is not None):
                visit(_field(node, "item"), bound, depth + 1, node_path)
            else:
                reasons.append("structured-invalid")
            expanded.add(expansion)

    try:
        for node in owner["nodes"]:
            add_counts(_field(node, "numberOfEpisodes"))
            if _field(node, "containsSeason") is not None:
                raise _Issue("catalog-conflict")
            visit(_field(node, "episode"), True)
            visit(_field(node, "hasPart"), False)
        for node in episodes:
            associated = parents(node, "partOfSeason") or parents(node, "partOfSeries")
            if selected in associated:
                add_episode(node)
        for raw in graph.nodes:
            node = raw
            if "ItemList" in _types(node) and selected in (
                parents(node, "partOfSeason") or parents(node, "partOfSeries")
            ):
                visit(node, True)
    except _Issue as issue:
        result = _empty([*reasons, str(issue)], str(issue) == "structured-limit")
        if str(issue) != "structured-limit":
            result["seriesMetadata"] = metadata
        return result
    if len(counts) > 1:
        reasons.append("conflicting-counts")
    return {
        "links": sorted(links.values(), key=lambda item: item["ordinal"]),
        "expectedCount": next(iter(counts)) if len(counts) == 1 else None,
        "linksTruncated": False,
        "reasons": list(dict.fromkeys(reasons)),
        "seriesMetadata": metadata,
        "coverUrls": [
            url for node in owner["nodes"]
            for image in _values(node.get("image"))[:4]
            if isinstance(url := (image.get("contentUrl") or image.get("url")
                                  if isinstance(image, dict) else image), str)
        ][:4],
        "catalogLink": selected if selected != graph.page_url else None,
        "discoverySources": ["json-ld"] if links else [],
    }


JSON_LD_SNAPSHOT = r"""({maxScripts,maxBytes}) => {
  const nodes=[...document.querySelectorAll('script[type]')].filter(node =>
    node.type.toLowerCase().split(';')[0].trim()==='application/ld+json');
  if(nodes.length>maxScripts) return {scripts:[],limited:true};
  const scripts=[]; let size=0;
  for(const node of nodes) {
    const content=node.textContent || '';
    if(content.length>maxBytes) return {scripts:[],limited:true};
    size+=new TextEncoder().encode(content).byteLength;
    if(size>maxBytes) return {scripts:[],limited:true};
    scripts.push(content);
  }
  return {scripts,limited:false};
}"""
