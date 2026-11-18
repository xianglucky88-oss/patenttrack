"""Single responsibility: surface field normalisation (the normalizer stage).

This stage deliberately does not decide whether two records are the same
application.  It only makes the surface form comparable:

* source names, display names and event fields are trimmed / case-folded;
* application, publication, grant and registration numbers are parsed into
  ``{region, namespace, body, canonical}`` structures for ``idmap``;
* names keep both the display value and a comparison-only ``name_key``.

Number normalisation rules
--------------------------
* Formatting characters (spaces, hyphens, dots, slashes, ...) are removed.
* A leading office/region code (CN, US, EP, WO, ...) is split from the body;
  the region is part of identifier identity and is never discarded.
* Publication / grant kind codes (``A``, ``B1``, ``U`` ...) are stripped from
  the *comparison core* so that "CN1234567A" and "CN1234567B1" still compare
  as the same publication document, while the raw value is retained.
* Application numbers keep their complete body; only formatting differs.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Iterable

# Offices that may legitimately appear as a two letter prefix.  A two letter
# prefix outside this set is treated as part of the number body rather than as
# a region, which avoids inventing regions from arbitrary alphanumeric data.
KNOWN_REGIONS = frozenset(
    {
        "AE", "AT", "AU", "BE", "BG", "BR", "CA", "CH", "CL", "CN", "CZ",
        "DE", "DK", "EA", "EE", "EG", "EP", "ES", "FI", "FR", "GB", "GR",
        "HK", "HR", "HU", "ID", "IE", "IL", "IN", "IS", "IT", "JP", "KR",
        "LT", "LU", "LV", "MA", "MC", "MX", "MY", "NL", "NO", "NZ", "OA",
        "PH", "PL", "PT", "RO", "RS", "RU", "SA", "SE", "SG", "SI", "SK",
        "TH", "TR", "TW", "UA", "US", "VN", "WO", "ZA",
    }
)

# Office spellings -> region.  Used for an explicit region/country field.
OFFICE_REGIONS = {
    "CNIPA": "CN", "SIPO": "CN", "CHINA": "CN", "中国": "CN", "中国专利": "CN",
    "USPTO": "US", "US": "US", "UNITED STATES": "US", "美国": "US",
    "EPO": "EP", "EP": "EP", "EUROPEAN PATENT OFFICE": "EP", "欧盟": "EP",
    "WIPO": "WO", "PCT": "WO", "WO": "WO", "世界知识产权组织": "WO",
    "JPO": "JP", "JP": "JP", "日本": "JP",
    "KIPO": "KR", "KR": "KR", "韩国": "KR",
    "DPMA": "DE", "DE": "DE", "UKIPO": "GB", "GB": "GB",
    "CIPO": "CA", "CA": "CA", "IP AUSTRALIA": "AU", "AU": "AU",
    "INPI": "FR", "FR": "FR", "IPOS": "SG", "SG": "SG",
}

# Source names that unambiguously identify an office.  The hint is only used
# when the record itself has no region field and the identifier has no prefix.
SOURCE_REGION_HINTS = {
    "cnipa": "CN", "sipo": "CN", "中国专利": "CN", "中国商标": "CN",
    "uspto": "US", "us-patent": "US", "us_trademark": "US",
    "epo": "EP", "欧盟": "EP", "wipo": "WO", "pct": "WO",
    "jpo": "JP", "kipo": "KR", "dpma": "DE", "ukipo": "GB",
    "inpi": "FR", "ipos": "SG", "cipo": "CA", "ip australia": "AU",
}

# Field aliases.  All aliases are collected instead of taking only the first,
# because different sources use different names for the same identifier.
NAMESPACE_FIELDS: dict[str, tuple[str, ...]] = {
    "application": (
        "app_no", "appno", "appnum", "app_number", "app",
        "application", "application_no", "application_number",
        "application_numbers", "申请号", "申请编号",
    ),
    "publication": (
        "pub_no", "pubno", "pubnum", "pub_number", "pub",
        "publication", "publication_no", "publication_number",
        "publication_numbers", "公开号", "公告号",
    ),
    "grant": (
        "grant_no", "grantno", "grantnum", "grant_number", "grant",
        "granted_no", "authorization_no", "authorisation_no",
        "patent_no", "patent_number", "授权号", "专利号",
    ),
    "registration": (
        "reg_no", "regno", "regnum", "registration_no", "registration_number",
        "trademark_no", "trademark_number", "mark_no", "注册号", "商标号",
    ),
    "priority": (
        "priority", "priority_no", "priority_number", "priority_numbers",
        "优先权号", "优先权",
    ),
}

NAMESPACE_CODES = {
    "application": "APP",
    "publication": "PUB",
    "grant": "GRANT",
    "registration": "REG",
    "priority": "PRIO",
}

_REGION_FIELDS = ("region", "country", "office", "jurisdiction", "authority", "国家", "地区")
_PUNCT_RE = re.compile(r"[^0-9A-Z㐀-鿿]+")
_DIGIT_RE = re.compile(r"\d")
_KIND_RE = re.compile(r"^(?P<base>[0-9][0-9A-Z]*?)(?P<kind>[A-Z][0-9]?)$")
_STRING_KEYS = ("name", "title", "invention_name", "商标名称", "名称")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip()


def normalize_text(value: Any) -> str:
    """Upper-case alphanumeric form used for identifier comparison."""
    text = unicodedata.normalize("NFKC", "" if value is None else str(value))
    return _PUNCT_RE.sub("", text.upper())


def name_key(value: Any) -> str:
    """Comparison key for names: Unicode-normalised, case-folded, compact."""
    text = unicodedata.normalize("NFKC", "" if value is None else str(value)).casefold()
    return "".join(ch for ch in text if ch.isalnum())


def name_tokens(value: Any) -> tuple[str, ...]:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value)).casefold()
    parts = [p for p in re.split(r"[^\w㐀-鿿]+", text, flags=re.UNICODE) if p]
    return tuple(parts)


def name_similarity(left: Any, right: Any) -> float:
    """Cheap name similarity used only as supporting evidence.

    Exact keys short-circuit.  For CJK names character bigrams are used; for
    other names token overlap plus a bounded SequenceMatcher ratio.
    """
    a, b = name_key(left), name_key(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if len(a) >= 2 and len(b) >= 2 and any("㐀" <= ch <= "鿿" for ch in a + b):
        grams_a = {a[i:i + 2] for i in range(len(a) - 1)}
        grams_b = {b[i:i + 2] for i in range(len(b) - 1)}
        if grams_a and grams_b:
            return len(grams_a & grams_b) / len(grams_a | grams_b)
        return 0.0
    ta, tb = set(name_tokens(left)), set(name_tokens(right))
    if ta and tb:
        jaccard = len(ta & tb) / len(ta | tb)
    else:
        jaccard = 0.0
    ratio = SequenceMatcher(None, a, b).ratio()
    return max(jaccard, ratio)


def parse_region(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    compact = normalize_text(text)
    if compact in OFFICE_REGIONS:
        return OFFICE_REGIONS[compact]
    if len(compact) >= 2 and compact[:2] in KNOWN_REGIONS:
        return compact[:2]
    return None


def explicit_region(record: dict[str, Any]) -> str | None:
    """Region stated by the record itself (strong evidence)."""
    for field in _REGION_FIELDS:
        region = parse_region(record.get(field))
        if region:
            return region
    return None


def source_region(record: dict[str, Any]) -> str | None:
    """Region inferred from the source name (weak evidence, used last)."""
    source = _text(record.get("src", "")).lower()
    return SOURCE_REGION_HINTS.get(source)


def infer_region(record: dict[str, Any]) -> str | None:
    return explicit_region(record) or source_region(record)


def _strip_kind(body: str) -> tuple[str, str | None]:
    """Split a publication/grant kind suffix from a numeric body.

    Only suffixes that look like a patent kind code are removed and the base
    must contain at least four characters and a digit.  This keeps arbitrary
    alphanumeric registration numbers intact.
    """
    if not body or not _DIGIT_RE.search(body):
        return body, None
    match = _KIND_RE.match(body)
    if not match:
        return body, None
    base, kind = match.group("base"), match.group("kind")
    if len(base) < 4 or not _DIGIT_RE.search(base):
        return body, None
    if not any(ch.isalpha() for ch in base):
        # Numeric-only base followed by a letter is a kind code (CN1234567A).
        return base, kind
    # Mixed alphanumeric body: only strip short, conventional kind suffixes.
    if len(kind) <= 2 and kind[0] in "ABCDEFGHJKLMNPRSTUWXY":
        return base, kind
    return body, None


def parse_identifier(
    raw: Any,
    namespace: str = "application",
    region_hint: str | None = None,
) -> dict[str, Any] | None:
    """Parse one identifier occurrence.

    Returns a mapping with ``region``, ``namespace``, ``body``, ``kind``,
    ``canonical`` (``REGION:NAMESPACE:body``) and ``display``.  ``None`` is
    returned for empty input; malformed input is retained as a best-effort
    body rather than raising.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        for key in ("value", "no", "number", "id", "code"):
            if key in raw:
                raw = raw[key]
                break
        else:
            return None
    if isinstance(raw, (list, tuple)):
        # A list at this level is handled by extract_identifiers; take first.
        if not raw:
            return None
        raw = raw[0]
    text = normalize_text(raw)
    if not text:
        return None

    region = parse_region(region_hint)
    body = text
    # Explicit region hint matching a prefix should be stripped even if the
    # prefix itself is not in KNOWN_REGIONS (e.g. odd source-local codes).
    if region and body.startswith(region):
        body = body[len(region):]
    elif body[:2] in KNOWN_REGIONS:
        region = body[:2]
        body = body[2:]
    else:
        # Treat a three letter office spelling such as EPO as the office.
        if body[:3] in OFFICE_REGIONS:
            region = OFFICE_REGIONS[body[:3]]
            body = body[3:]
        else:
            region = region or "??"

    if not body:
        body = text
    kind: str | None = None
    if namespace in ("publication", "grant"):
        body, kind = _strip_kind(body)
    if not body:
        return None
    if region == "??":
        display = body
        canonical_region = "??"
    else:
        display = f"{region}{body}"
        canonical_region = region
    code = NAMESPACE_CODES.get(namespace, namespace.upper())
    return {
        "namespace": namespace,
        "region": canonical_region,
        "body": body,
        "kind": kind,
        "raw": _text(raw),
        "display": display,
        "canonical": f"{canonical_region}:{code}:{body}",
    }


def _iter_values(value: Any) -> Iterable[Any]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        return [item for item in value if item is not None]
    if isinstance(value, dict):
        # A source may return {"value": "..."} or {"no": "..."}.
        for key in ("value", "no", "number", "id", "code"):
            if key in value:
                return [value[key]]
        return [value]
    return [value]


def _add_identifier(
    found: dict[str, list[dict[str, Any]]],
    raw: Any,
    namespace: str,
    region_hint: str | None,
) -> None:
    parsed = parse_identifier(raw, namespace, region_hint)
    if not parsed:
        return
    bucket = found.setdefault(namespace, [])
    seen = {(item["canonical"], item["raw"]) for item in bucket}
    if (parsed["canonical"], parsed["raw"]) not in seen:
        bucket.append(parsed)


def extract_identifiers(record: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Collect all identifier occurrences in a record, grouped by namespace.

    Region evidence is resolved in priority order: the number's own prefix
    (strongest, attached to the number itself), then an explicit record
    region/country field, then a well-known source-name hint.  Using the
    prefix first means ``app_no="US1234567"`` from a Chinese source is still
    a US application.
    """
    found: dict[str, list[dict[str, Any]]] = {}
    stated = explicit_region(record)
    hinted = source_region(record)

    for namespace, fields in NAMESPACE_FIELDS.items():
        for field in fields:
            if field in record:
                for value in _iter_values(record.get(field)):
                    _add_identifier(found, value, namespace, stated or hinted)

    # Optional canonical container: {"application": ..., "publication": ...}
    container = record.get("ids") or record.get("identifiers")
    if isinstance(container, dict):
        for key, value in container.items():
            namespace = _namespace_of(key)
            if namespace:
                for value_item in _iter_values(value):
                    _add_identifier(found, value_item, namespace, stated or hinted)
    elif isinstance(container, list):
        for item in container:
            if isinstance(item, dict):
                namespace = _namespace_of(
                    item.get("namespace") or item.get("type") or item.get("kind") or ""
                )
                value = item.get("value") or item.get("no") or item.get("number")
                if namespace and value is not None:
                    _add_identifier(found, value, namespace, stated or hinted)
            else:
                _add_identifier(found, item, "application", stated or hinted)

    # Stable ordering makes evidence and batch grouping deterministic.
    for namespace, items in found.items():
        items.sort(key=lambda item: (item["canonical"], item["raw"]))
    return found


def _namespace_of(value: Any) -> str | None:
    key = _text(value).lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "app": "application", "application": "application", "appno": "application",
        "application_no": "application", "application_number": "application",
        "pub": "publication", "publication": "publication", "publication_no": "publication",
        "publication_number": "publication", "public": "publication",
        "grant": "grant", "granted": "grant", "grant_no": "grant",
        "authorization": "grant", "authorisation": "grant", "patent": "grant",
        "reg": "registration", "registration": "registration",
        "registration_no": "registration", "trademark": "registration",
        "trademark_no": "registration",
        "priority": "priority", "prio": "priority",
    }
    return aliases.get(key)


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        text = _text(value)
        if not text:
            return 0
        try:
            return int(float(text))
        except (TypeError, ValueError):
            return 0


def record_fingerprint(record: dict[str, Any], fields: Iterable[str] | None = None) -> str:
    """Stable content fingerprint used as a weak fallback identity.

    Volatile bookkeeping fields are excluded so repeated messages fingerprint
    the same.  The fingerprint is only used when no real identifier exists.
    """
    skip = {
        "dedup", "idmap", "family", "legal_event", "legal_status", "ids",
        "canonical", "canonical_ids", "name_key", "name_tokens", "region",
        "app_no_canonical", "pub_no_canonical", "grant_no_canonical",
        "registration_no_canonical", "priority_no_canonical", "_value_raw",
        "_stage_query_page", "_stage_idmap", "_stage_family",
        "_stage_legal_status", "_stage_dedup", "_emit", "_store_key",
    }
    if fields is None:
        payload = {k: v for k, v in record.items() if k not in skip and not str(k).startswith("_")}
    else:
        payload = {k: record.get(k) for k in fields}
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


def process(record: dict[str, Any]) -> dict[str, Any]:
    """Normalise one record without mutating the input."""
    rec = dict(record)
    rec["src"] = _text(rec.get("src", "")).lower()
    display_name = next((_text(rec.get(field)) for field in _STRING_KEYS if _text(rec.get(field))), "")
    rec["name"] = display_name
    raw_value = rec.get("value", 0)
    rec["value"] = _to_int(raw_value)
    if "value" in rec and _text(raw_value) and rec["value"] == 0:
        rec["_value_raw"] = raw_value

    region = infer_region(rec)
    if region:
        rec["region"] = region
    else:
        rec.setdefault("region", "??")

    rec["name_key"] = name_key(display_name)
    rec["name_tokens"] = list(name_tokens(display_name))

    identifiers = extract_identifiers(rec)
    rec["ids"] = identifiers
    canonical_ids: dict[str, list[str]] = {}
    for namespace, items in identifiers.items():
        values = sorted({item["canonical"] for item in items})
        canonical_ids[namespace] = values
        if values:
            rec[f"{namespace}_no_canonical"] = values[0]
    rec["canonical_ids"] = canonical_ids
    # Always expose the canonical namespace keys, even when absent, so downstream
    # stages can test them without KeyError.
    for namespace in ("application", "publication", "grant", "registration", "priority"):
        rec.setdefault(f"{namespace}_no_canonical", None)

    rec.setdefault("_stage_normalizer", "ok")
    return rec


def process_batch(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [process(record) for record in records]
