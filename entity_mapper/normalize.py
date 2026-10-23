"""Name, country and identifier normalisation.

Everything here is deterministic and offline. Keep the raw value alongside the
cleaned one: we match on cleaned names but always display raw names.
"""
from __future__ import annotations

import re
from functools import lru_cache

from cleanco import basename
from unidecode import unidecode

try:
    import pycountry
except ImportError:  # optional
    pycountry = None

# Abbreviations expanded before matching. Keep this list conservative:
# an over-eager expansion creates false positives.
ABBREVIATIONS = {
    "intl": "international", "int'l": "international", "internat": "international",
    "mgmt": "management", "mgt": "management",
    "hldgs": "holdings", "hldg": "holding", "hlds": "holdings",
    "grp": "group",
    "svcs": "services", "svc": "service", "servs": "services",
    "natl": "national", "nat'l": "national",
    "assoc": "associates", "assocs": "associates",
    "bros": "brothers",
    "fin": "financial", "finl": "financial",
    "invest": "investment", "invts": "investments", "inv": "investment",
    "mfg": "manufacturing",
    "dev": "development",
    "ptnrs": "partners", "prtnrs": "partners",
    "cap": "capital",
    "sec": "securities", "secs": "securities",
    "bk": "bank",
}

# Separators that mean "this string holds more than one name".
_ALT_NAME_SPLIT = re.compile(
    r"\s+(?:t/a|trading as|d/b/a|dba|f/k/a|fka|formerly(?: known as)?|a/k/a|aka|c/o)\s+",
    re.IGNORECASE,
)
_PARENS = re.compile(r"\(([^)]*)\)")
_ALT_MARKER = re.compile(r"^(formerly|fka|f/k/a|aka|a/k/a|t/a|trading as|previously|now|dba|d/b/a)\b", re.I)
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")
_DIGITS = re.compile(r"\d+")


_LEGAL_WORDS_EXTRA = {"srl", "sro", "kft", "zrt", "berhad", "co", "company", "corp", "corporation", "incorporated", "limited", "ltd", "llc",
                      "plc", "inc", "rl", "de", "cv", "sab", "sapi", "and", "kg", "kgaa", "gmbh", "ag", "sa", "nv", "bv", "spa", "srl", "sarl", "sas",
                      "oy", "oyj", "ab", "as", "asa", "aps", "se", "pte", "pty", "lp", "llp", "pc", "bhd",
                      "sdn", "tbk", "pt", "jsc", "pjsc", "ojsc", "ooo", "zao", "sl", "sau", "ltda", "kk"}


def _norm_tok(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", t.lower())


def _strip_legal_tail(s: str) -> str:
    """Drop legal-form words from the end ('Gmbh & Co. Kg', 'S.A.B. de C.V.', 'UK Limited' keeps 'UK')
    and a leading 'PT' (Indonesia). Country words are kept on purpose."""
    toks = s.replace(",", " ").split()
    while len(toks) > 1 and (_norm_tok(toks[-1]) in _LEGAL_WORDS_EXTRA or _norm_tok(toks[-1]) in ("", "tbk")):
        toks.pop()
    if len(toks) > 1 and _norm_tok(toks[0]) == "pt":
        toks = toks[1:]
    return " ".join(toks)


def split_variants(raw: str) -> list[str]:
    """'ABC Ltd t/a Foo (formerly Bar Ltd)' -> ['ABC Ltd', 'Foo', 'Bar Ltd']."""
    if not isinstance(raw, str) or not raw.strip():
        return []
    raw = unidecode(raw).strip()
    # Brackets hold an alternative name only when marked ("(formerly X)", "(t/a Y)").
    # "(Malaysia)", "(Jersey)", "(West)" are part of the name, not a second company name.
    extras = [m.strip() for m in _PARENS.findall(raw) if _ALT_MARKER.match(m.strip())]
    main = _PARENS.sub(lambda m: " " if _ALT_MARKER.match(m.group(1).strip()) else f" {m.group(1)} ", raw)
    parts = [p.strip() for p in _ALT_NAME_SPLIT.split(main) if p and p.strip()]
    for e in extras:
        e = re.sub(r"^(?:formerly(?: known as)?|fka|f/k/a|t/a|trading as|aka)\s+", "", e, flags=re.I)
        if len(e) > 2 and not e.lower() in {"uk", "us", "usa"}:
            parts.append(e)
    # de-duplicate, keep order
    seen, out = set(), []
    for p in parts:
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


# Legal forms written out in full, and web suffixes. Mapped to their family for legal_form().
_LONG_FORMS = [
    (r"kommanditgesellschaft auf aktien", "KGAA"), (r"gesellschaft mit beschr(a|ae)nkter haftung", "GMBH"),
    (r"aktiengesellschaft", "AG"), (r"societe anonyme", "SA"), (r"sociedad anonima", "SA"),
    (r"sociedade anonima", "SA"), (r"societa per azioni", "SPA"), (r"public limited company", "PLC"),
    (r"limited liability company", "LLC"), (r"limited liability partnership", "LLP"),
    (r"naamloze vennootschap", "NV"), (r"besloten vennootschap", "BV"), (r"aktiebolag", "AB"),
    (r"s\.? ?a\.? ?r\.? ?l\.?", "SARL"), (r"kgaa", "KGAA"), (r"societas europaea", "SE"),
]
_LONG_FORM_RE = [(re.compile(rf"[\s,]+{p}\.?\s*$", re.I), f) for p, f in _LONG_FORMS]
_WEB_SUFFIX = re.compile(r"\.(com|net|org|co\.uk|io)\b", re.I)


def _strip_long_form(s: str) -> tuple[str, str]:
    for rx, fam in _LONG_FORM_RE:
        m = rx.search(s)
        if m:
            return s[:m.start()].strip(), fam
    return s, ""


@lru_cache(maxsize=500_000)
def clean_name(raw: str) -> str:
    """Canonical comparison form of a single company name."""
    if not isinstance(raw, str):
        return ""
    s = unidecode(raw).strip()
    s = _WEB_SUFFIX.sub("", s)          # "Amazon.com, Inc." -> "Amazon, Inc."
    s, _ = _strip_long_form(s)          # "Siemens Aktiengesellschaft" -> "Siemens"
    s = _strip_legal_tail(s)
    # cleanco twice: "Foo Co., Limited" -> "Foo Co." -> "Foo". Only accept the cut when every
    # removed word is a legal form: cleanco also drops country words ("Unilever UK" -> "Unilever"),
    # which would silently turn a subsidiary into its group.
    for _ in range(2):
        b = basename(s)
        if not b or b == s:
            break
        removed = re.sub(r"[^a-z0-9/ ]", " ", s[len(b):].lower() if s.lower().startswith(b.lower()) else "?").split()
        if not removed or not all(w in _LEGAL_WORDS for w in removed):
            break
        s = b
    s = s.lower().replace("&", " and ").replace("+", " and ")
    s = s.replace("'", "")
    s = _NON_ALNUM.sub(" ", s)
    tokens = [ABBREVIATIONS.get(t, t) for t in s.split()]
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    s = " ".join(tokens)
    return _WS.sub(" ", s).strip()


def digit_tokens(clean: str) -> frozenset[str]:
    """Numbers in a name ('Fund 2', 'SPV 14'). Different numbers = different entities."""
    return frozenset(_DIGITS.findall(clean))


_COUNTRY_ALIASES = {
    "uk": "GB", "u.k.": "GB", "united kingdom": "GB", "great britain": "GB", "england": "GB",
    "scotland": "GB", "wales": "GB", "northern ireland": "GB",
    "usa": "US", "u.s.a.": "US", "u.s.": "US", "united states": "US", "united states of america": "US",
    "uae": "AE", "south korea": "KR", "korea": "KR", "russia": "RU", "hong kong": "HK",
    "the netherlands": "NL", "holland": "NL", "czech republic": "CZ", "turkey": "TR", "turkiye": "TR",
    "jersey": "JE", "guernsey": "GG", "isle of man": "IM", "cayman islands": "KY", "cayman": "KY",
    "bvi": "VG", "british virgin islands": "VG", "luxemburg": "LU",
    "ivory coast": "CI", "cote d'ivoire": "CI", "macedonia": "MK", "north macedonia": "MK",
    "trinidad & tobago": "TT", "trinidad and tobago": "TT", "vietnam": "VN", "taiwan": "TW",
    "iran": "IR", "syria": "SY", "laos": "LA", "bolivia": "BO", "venezuela": "VE",
    "tanzania": "TZ", "moldova": "MD", "palestine": "PS", "kosovo": "XK", "uae": "AE",
}
# Sub-national labels some in-house systems use ("USA West", "USA New York", "UK - London").
_COUNTRY_PREFIXES = [("usa ", "US"), ("us ", "US"), ("united states ", "US"), ("uk ", "GB"),
                     ("uk-", "GB"), ("canada ", "CA"), ("china ", "CN")]


@lru_cache(maxsize=10_000)
def normalize_country(value) -> str:
    """Return ISO 3166 alpha-2, or '' if unknown."""
    if not isinstance(value, str) or not value.strip():
        return ""
    v = value.strip()
    low = v.lower()
    if low in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[low]
    for prefix, iso in _COUNTRY_PREFIXES:
        if low.startswith(prefix):
            return iso
    if len(v) == 2 and v.isalpha():
        return v.upper()
    if pycountry is not None:
        try:
            c = pycountry.countries.lookup(v)
            return c.alpha_2
        except LookupError:
            pass
    return ""


def lei_is_valid(lei) -> bool:
    """ISO 17442: 20 alphanumerics, ISO 7064 MOD 97-10 check digits."""
    if not isinstance(lei, str):
        return False
    lei = lei.strip().upper()
    if len(lei) != 20 or not lei.isalnum():
        return False
    digits = "".join(str(int(c, 36)) for c in lei)
    return int(digits) % 97 == 1


def clean_id(value) -> str:
    if value is None:
        return ""
    s = str(value).strip().upper()
    if s in {"", "NAN", "NONE", "NULL", "N/A", "NA", "-", "--", "#N/A", "0"}:
        return ""
    if s.endswith(".0") and s[:-2].isdigit():  # Excel turned an ID into a float
        s = s[:-2]
    return s


# Legal form families. Differing families (LLP vs Ltd, LP vs Inc) often mean
# different entities, so a mismatch blocks auto-matching but is not fatal:
# bank files frequently get the legal form wrong.
_LEGAL_FORMS = {
    "ltd": "LTD", "limited": "LTD", "ltda": "LTD",
    "plc": "PLC", "llp": "LLP", "lp": "LP", "llc": "LLC", "l.l.c.": "LLC",
    "inc": "INC", "incorporated": "INC", "corp": "INC", "corporation": "INC",
    "gmbh": "GMBH", "ag": "AG", "sa": "SA", "s.a.": "SA", "sas": "SAS", "sarl": "SARL",
    "s.a.r.l.": "SARL", "bv": "BV", "b.v.": "BV", "nv": "NV", "n.v.": "NV", "dac": "DAC",
    "scsp": "SCSP", "scs": "SCS", "spa": "SPA", "s.p.a.": "SPA", "ab": "AB", "as": "AS",
    "srl": "SRL", "s.r.l.": "SRL", "sro": "SRO", "s.r.o.": "SRO", "kft": "KFT", "zrt": "ZRT",
    "sdn": "SDNBHD", "bhd": "BHD", "berhad": "BHD", "tbk": "TBK", "jsc": "JSC", "pjsc": "JSC",
    "oy": "OY", "oyj": "OY", "kg": "KG", "kgaa": "KGAA", "se": "SE", "asa": "AS",
    "pte": "LTD", "pty": "LTD", "sa/nv": "SA",
}


_LEGAL_WORDS = _LEGAL_WORDS_EXTRA | {k.replace(".", "") for k in _LEGAL_FORMS}


def legal_form(raw) -> str:
    """Canonical legal form family from the end of a raw name, or ''."""
    if not isinstance(raw, str):
        return ""
    _, fam = _strip_long_form(unidecode(raw).strip())
    if fam:
        return fam
    toks = re.sub(r"[,()]", " ", unidecode(raw).lower()).split()
    for t in reversed(toks[-3:]):
        f = _LEGAL_FORMS.get(t) or _LEGAL_FORMS.get(t.rstrip("."))
        if f:
            return f
    return ""


_COMPATIBLE = [{"LTD", "PLC"}, {"AG", "SA"}]  # SA/AG: Swiss bilingual forms


def forms_compatible(a: str, b: str) -> bool:
    if not a or not b or a == b:
        return True
    return any(a in g and b in g for g in _COMPATIBLE)


# Where each legal form is used. Only forms with a narrow footprint are listed:
# "Ltd", "Inc" and "SA" are used too widely to say anything about country.
FORM_COUNTRIES = {
    "GMBH": {"DE", "AT", "CH", "LI", "LU"},
    "AG": {"DE", "AT", "CH", "LI"},
    "KG": {"DE", "AT", "LI"},
    "KGAA": {"DE", "LU"},
    "BV": {"NL", "BE", "CW", "SX"},
    "NV": {"NL", "BE", "CW", "SX"},
    "SARL": {"LU", "FR", "CH", "BE", "MA", "TN"},
    "SAS": {"FR", "CO", "LU", "BE"},
    "SCSP": {"LU"},
    "SCS": {"LU", "BE"},
    "DAC": {"IE"},
    "PLC": {"GB", "IE", "JE", "GG", "IM", "NG", "LK", "MY"},
    "LLP": {"GB", "US", "IN", "JE", "GG", "SG", "KZ"},
    "SPA": {"IT"},
    "SRL": {"IT", "RO", "BE", "MD", "AR", "UY", "PE", "BO", "DO", "CR"},
    "SRO": {"CZ", "SK"},
    "KFT": {"HU"}, "ZRT": {"HU"},
    "SDNBHD": {"MY"}, "TBK": {"ID"},
    "OY": {"FI"},
    "AB": {"SE", "FI"},
}


def form_country_ok(form: str, country: str):
    """False if the legal form is not used in `country`; None if the form says nothing."""
    if not form or not country or form not in FORM_COUNTRIES:
        return None
    return country in FORM_COUNTRIES[form]


# Jurisdictions often used for incorporation while the group is headquartered elsewhere.
# In-house lists usually hold HQ country; banks often send country of incorporation.
INCORPORATION_HUBS = {"JE", "GG", "IM", "IE", "LU", "NL", "KY", "BM", "VG", "CY", "MT", "CW",
                      "PA", "MU", "LI", "BS", "GI", "MH", "LR"}
