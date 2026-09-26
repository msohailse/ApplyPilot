"""Lightweight job classification: language, employment type, country, work mode.

Deterministic, regex-based extraction from job descriptions and locations.
Used to tag and filter jobs on the dashboard (e.g. "no language required",
"full-time", "remote", country).

No LLM calls — fast enough to run as a lazy backfill over existing rows and
inline during enrichment for new jobs.
"""

import json
import logging
import re
import sqlite3

log = logging.getLogger(__name__)

# Language names we can detect in a job description. English is intentionally
# excluded — it is the baseline language, so we only flag *additional*
# languages (French, Spanish, etc.).
_LANGUAGE_LABELS: dict[str, str] = {
    "french": "French",
    "spanish": "Spanish",
    "german": "German",
    "italian": "Italian",
    "portuguese": "Portuguese",
    "dutch": "Dutch",
    "mandarin": "Mandarin",
    "cantonese": "Cantonese",
    "chinese": "Chinese",
    "japanese": "Japanese",
    "korean": "Korean",
    "arabic": "Arabic",
    "russian": "Russian",
    "polish": "Polish",
    "hindi": "Hindi",
    "norwegian": "Norwegian",
    "welsh": "Welsh",
}

# Words/phrases that, when appearing near a language name, indicate that the
# language matters for the role.
_REQ_CONTEXT = (
    "fluent", "fluency", "proficien", "required", "mandatory", "essential",
    "speak", "speaking", "spoken", "language", "languages", "bilingual",
    "asset", "advantage", "knowledge of", "working knowledge", "command of",
    "written and oral", "oral and written", "written and spoken", "conversational",
)

_WINDOW = 70


def detect_language_requirement(text: str | None) -> str | None:
    """Return the language required (if any) from a job description.

    Returns a human-readable label such as "French" or "Bilingual (EN/FR)",
    or None when no additional language requirement is detected.
    """
    if not text:
        return None

    t = text.lower()

    # Bilingual implies English + another language (EN/FR in Canada).
    if "bilingual" in t and not re.search(
        r"\b(?:no|not)\s+bilingual\b|\bbilingual\s+(?:is\s+)?not\b", t
    ):
        return "Bilingual (EN/FR)"

    # Explicit "English and French" / "French and English".
    if re.search(r"\benglish\s*(?:and|&|/)\s*french\b", t) or re.search(
        r"\bfrench\s*(?:and|&|/)\s*english\b", t
    ):
        return "Bilingual (EN/FR)"

    for key, label in _LANGUAGE_LABELS.items():
        for m in re.finditer(rf"\b{key}\b", t):
            window = t[max(0, m.start() - _WINDOW):m.end() + _WINDOW]
            if any(ctx in window for ctx in _REQ_CONTEXT):
                return label

    return None


def detect_employment_type(text: str | None) -> str | None:
    """Return the employment type (full-time, part-time, contract, etc.)."""
    if not text:
        return None

    t = text.lower()

    detected: list[str] = []
    if re.search(r"\bpart[- ]?time\b", t):
        detected.append("Part-time")
    if re.search(r"\bfull[- ]?time\b", t):
        detected.append("Full-time")
    if re.search(r"\bcontract(?:ual|or)?\b|\btemporary\b|\bfixed[- ]?term\b", t):
        detected.append("Contract")
    if re.search(r"\bintern(?:ship)?\b", t):
        detected.append("Internship")
    if re.search(r"\bco[- ]?op\b", t):
        detected.append("Co-op")
    if re.search(r"\bfreelance\b", t):
        detected.append("Freelance")
    if re.search(r"\bcasual\b", t):
        detected.append("Casual")
    if re.search(r"\bpermanent\b", t):
        detected.append("Permanent")

    if not detected:
        return None

    if "Part-time" in detected and "Full-time" in detected:
        return "Full-time / Part-time"

    # Prefer the distinctions the user cares about most.
    for preferred in ("Part-time", "Full-time", "Contract", "Internship",
                      "Co-op", "Freelance", "Casual", "Permanent"):
        if preferred in detected:
            return preferred

    return None


def classify_text(text: str | None) -> dict[str, str | None]:
    """Classify a single description into language and employment type."""
    return {
        "language_requirement": detect_language_requirement(text),
        "employment_type": detect_employment_type(text),
    }


# ---------------------------------------------------------------------------
# Country detection
# ---------------------------------------------------------------------------

# Full country names (longest matched first to avoid partial overlaps).
_COUNTRY_NAMES: dict[str, str] = {
    "united states of america": "United States",
    "united states": "United States",
    "united kingdom": "United Kingdom",
    "great britain": "United Kingdom",
    "northern ireland": "United Kingdom",
    "uk": "United Kingdom",
    "czech republic": "Czechia",
    "south korea": "South Korea",
    "new zealand": "New Zealand",
    "south africa": "South Africa",
    "united arab emirates": "United Arab Emirates",
    "saudi arabia": "Saudi Arabia",
    "usa": "United States",
    "england": "United Kingdom",
    "scotland": "United Kingdom",
    "wales": "United Kingdom",
    "canada": "Canada",
    "germany": "Germany",
    "france": "France",
    "italy": "Italy",
    "spain": "Spain",
    "portugal": "Portugal",
    "netherlands": "Netherlands",
    "holland": "Netherlands",
    "ireland": "Ireland",
    "sweden": "Sweden",
    "norway": "Norway",
    "denmark": "Denmark",
    "finland": "Finland",
    "belgium": "Belgium",
    "switzerland": "Switzerland",
    "austria": "Austria",
    "poland": "Poland",
    "czechia": "Czechia",
    "romania": "Romania",
    "hungary": "Hungary",
    "greece": "Greece",
    "australia": "Australia",
    "brazil": "Brazil",
    "mexico": "Mexico",
    "india": "India",
    "japan": "Japan",
    "china": "China",
    "singapore": "Singapore",
    "israel": "Israel",
    "turkey": "Turkey",
    "estonia": "Estonia",
    "latvia": "Latvia",
    "lithuania": "Lithuania",
    "croatia": "Croatia",
    "slovenia": "Slovenia",
    "slovakia": "Slovakia",
    "bulgaria": "Bulgaria",
    "luxembourg": "Luxembourg",
    "iceland": "Iceland",
    "malta": "Malta",
    "cyprus": "Cyprus",
    "serbia": "Serbia",
    "ukraine": "Ukraine",
    "colombia": "Colombia",
    "chile": "Chile",
    "argentina": "Argentina",
    "peru": "Peru",
    "vietnam": "Vietnam",
    "thailand": "Thailand",
    "malaysia": "Malaysia",
    "indonesia": "Indonesia",
    "philippines": "Philippines",
    "pakistan": "Pakistan",
    "bangladesh": "Bangladesh",
    "nigeria": "Nigeria",
    "kenya": "Kenya",
    "egypt": "Egypt",
    "morocco": "Morocco",
    "qatar": "Qatar",
    "taiwan": "Taiwan",
    "hong kong": "Hong Kong",
}

# ISO country codes that appear as standalone tokens in location strings.
# Only unambiguous codes are included (US state abbreviations like "CA", "MA",
# "NY" are deliberately excluded).
_COUNTRY_CODES: dict[str, str] = {
    "us": "United States",
    "gb": "United Kingdom",
    "de": "Germany",
    "fr": "France",
    "it": "Italy",
    "es": "Spain",
    "pt": "Portugal",
    "nl": "Netherlands",
    "ie": "Ireland",
    "se": "Sweden",
    "no": "Norway",
    "dk": "Denmark",
    "fi": "Finland",
    "be": "Belgium",
    "ch": "Switzerland",
    "at": "Austria",
    "pl": "Poland",
    "cz": "Czechia",
    "ro": "Romania",
    "hu": "Hungary",
    "gr": "Greece",
    "au": "Australia",
    "nz": "New Zealand",
    "br": "Brazil",
    "mx": "Mexico",
    "jp": "Japan",
    "cn": "China",
    "sg": "Singapore",
    "kr": "South Korea",
    "ae": "United Arab Emirates",
    "can": "Canada",
    "tr": "Turkey",
    "aus": "Australia",
}

_COUNTRY_NAMES_SORTED = sorted(_COUNTRY_NAMES, key=len, reverse=True)

# US states / territories and Canadian provinces -> country. Checked before the
# city map so "Illinois Remote Work" resolves to United States without an LLM.
_STATE_PROVINCES: dict[str, str] = {
    # United States
    "alabama": "United States", "alaska": "United States", "arizona": "United States",
    "arkansas": "United States", "california": "United States", "colorado": "United States",
    "connecticut": "United States", "delaware": "United States", "florida": "United States",
    "georgia": "United States", "hawaii": "United States", "idaho": "United States",
    "illinois": "United States", "indiana": "United States", "iowa": "United States",
    "kansas": "United States", "kentucky": "United States", "louisiana": "United States",
    "maine": "United States", "maryland": "United States", "massachusetts": "United States",
    "michigan": "United States", "minnesota": "United States", "mississippi": "United States",
    "missouri": "United States", "montana": "United States", "nebraska": "United States",
    "nevada": "United States", "new hampshire": "United States", "new jersey": "United States",
    "new mexico": "United States", "new york": "United States", "north carolina": "United States",
    "north dakota": "United States", "ohio": "United States", "oklahoma": "United States",
    "oregon": "United States", "pennsylvania": "United States", "rhode island": "United States",
    "south carolina": "United States", "south dakota": "United States", "tennessee": "United States",
    "texas": "United States", "utah": "United States", "vermont": "United States",
    "virginia": "United States", "washington": "United States", "west virginia": "United States",
    "wisconsin": "United States", "wyoming": "United States",
    "district of columbia": "United States", "washington dc": "United States",
    "puerto rico": "United States",
    # Canada
    "ontario": "Canada", "quebec": "Canada", "british columbia": "Canada",
    "alberta": "Canada", "manitoba": "Canada", "saskatchewan": "Canada",
    "nova scotia": "Canada", "new brunswick": "Canada", "newfoundland": "Canada",
    "prince edward island": "Canada",
}

# Major world cities -> country (word-boundary matched). Covers city-only
# locations like "Milan" that the country/state maps can't resolve.
_CITY_COUNTRIES: dict[str, str] = {
    # Italy
    "milan": "Italy", "milano": "Italy", "rome": "Italy", "roma": "Italy",
    "turin": "Italy", "torino": "Italy", "bologna": "Italy", "florence": "Italy",
    "naples": "Italy", "venice": "Italy", "genoa": "Italy",
    # Norway
    "oslo": "Norway", "bergen": "Norway", "trondheim": "Norway", "stavanger": "Norway",
    "tromsø": "Norway", "tromso": "Norway", "drammen": "Norway", "fredrikstad": "Norway",
    "kristiansand": "Norway", "sandnes": "Norway", "sarpsborg": "Norway", "skien": "Norway",
    "ålesund": "Norway", "alesund": "Norway", "sandefjord": "Norway", "haugesund": "Norway",
    "tønsberg": "Norway", "tonsberg": "Norway", "moss": "Norway", "porsgrunn": "Norway",
    "bodø": "Norway", "bodo": "Norway", "arendal": "Norway", "hamar": "Norway",
    "larvik": "Norway", "halden": "Norway", "lillehammer": "Norway", "molde": "Norway",
    "harstad": "Norway", "gjøvik": "Norway", "gjovik": "Norway", "kongsberg": "Norway",
    "steinkjer": "Norway", "narvik": "Norway", "alta": "Norway", "horten": "Norway",
    "askøy": "Norway", "askoy": "Norway", "elverum": "Norway", "førde": "Norway",
    "forde": "Norway", "kristiansund": "Norway", "levanger": "Norway", "namsos": "Norway",
    "vadsø": "Norway", "vadso": "Norway", "hammerfest": "Norway", "kirkenes": "Norway",
    "egersund": "Norway", "flekkefjord": "Norway", "voss": "Norway", "stord": "Norway",
    "leirvik": "Norway", "notodden": "Norway", "kongsvinger": "Norway", "mo i rana": "Norway",
    "fauske": "Norway", "lysaker": "Norway", "fornebu": "Norway", "bærum": "Norway",
    "baerum": "Norway", "lørenskog": "Norway", "lorenskog": "Norway", "jessheim": "Norway",
    "grimstad": "Norway", "fyllingsdalen": "Norway", "askim": "Norway", "sandvika": "Norway",
    "asker": "Norway", "raufoss": "Norway", "vardø": "Norway", "vardo": "Norway",
    "longyearbyen": "Norway", "svolvær": "Norway", "svolvaer": "Norway", "orkanger": "Norway",
    "stjørdal": "Norway", "stjordal": "Norway", "nesodden": "Norway", "brumunddal": "Norway",
    "moelv": "Norway", "røros": "Norway", "roros": "Norway", "oppdal": "Norway",
    "orkdal": "Norway", "florø": "Norway", "floro": "Norway", "måløy": "Norway",
    "maloy": "Norway", "sogndal": "Norway", "vinstra": "Norway", "mandal": "Norway",
    "farsund": "Norway", "lyngdal": "Norway", "vennesla": "Norway", "risør": "Norway",
    "risor": "Norway", "tvedestrand": "Norway", "lillesand": "Norway", "evje": "Norway",
    "klepp": "Norway", "bryne": "Norway", "åkra": "Norway",
    "sola": "Norway", "randaberg": "Norway", "kvitsøy": "Norway",
    "utsira": "Norway", "karmøy": "Norway", "karmoy": "Norway", "kopervik": "Norway",
    "åkrehamn": "Norway", "skudeneshavn": "Norway", "bømlo": "Norway", "bomlo": "Norway",
    "fitjar": "Norway", "etne": "Norway", "ølen": "Norway", "oden": "Norway",
    "rosendal": "Norway", "ulvik": "Norway", "eidfjord": "Norway", "granvin": "Norway",
    "vaksdal": "Norway", "osterøy": "Norway", "osteroy": "Norway", "lindås": "Norway",
    "lindas": "Norway", "knarvik": "Norway", "isane": "Norway", "måløy": "Norway",
    "nordfjordeid": "Norway", "stryn": "Norway", "olden": "Norway", "loen": "Norway",
    "geiranger": "Norway", "stranda": "Norway", "hovden": "Norway", "sykkylven": "Norway",
    "ørsta": "Norway", "orsta": "Norway", "volda": "Norway", "fosnavåg": "Norway",
    "måløy": "Norway", "ulsteinvik": "Norway", "hareid": "Norway", "sunde": "Norway",
    # United Kingdom
    "london": "United Kingdom", "manchester": "United Kingdom", "birmingham": "United Kingdom",
    "edinburgh": "United Kingdom", "glasgow": "United Kingdom", "cambridge": "United Kingdom",
    "oxford": "United Kingdom", "bristol": "United Kingdom", "leeds": "United Kingdom",
    "liverpool": "United Kingdom", "cardiff": "United Kingdom", "belfast": "United Kingdom",
    "hemel hempstead": "United Kingdom", "uxbridge": "United Kingdom",
    # France
    "paris": "France", "lyon": "France", "marseille": "France", "toulouse": "France",
    "nice": "France",
    # Germany
    "berlin": "Germany", "munich": "Germany", "hamburg": "Germany", "cologne": "Germany",
    "frankfurt": "Germany", "stuttgart": "Germany", "hannover": "Germany", "leipzig": "Germany",
    # Ireland
    "dublin": "Ireland", "cork": "Ireland", "galway": "Ireland",
    # Sweden
    "stockholm": "Sweden", "gothenburg": "Sweden", "malmo": "Sweden",
    # Denmark
    "copenhagen": "Denmark",
    # Finland
    "helsinki": "Finland", "espoo": "Finland", "tampere": "Finland",
    # Netherlands
    "amsterdam": "Netherlands", "rotterdam": "Netherlands", "utrecht": "Netherlands",
    "the hague": "Netherlands", "eindhoven": "Netherlands",
    # Portugal
    "lisbon": "Portugal", "porto": "Portugal",
    # Spain
    "madrid": "Spain", "barcelona": "Spain", "valencia": "Spain", "seville": "Spain",
    # Austria
    "vienna": "Austria",
    # Belgium
    "brussels": "Belgium", "antwerp": "Belgium",
    # Switzerland
    "zurich": "Switzerland", "geneva": "Switzerland", "basel": "Switzerland",
    # Poland
    "warsaw": "Poland", "krakow": "Poland", "gdansk": "Poland",
    # Czechia
    "prague": "Czechia",
    # Hungary
    "budapest": "Hungary",
    # Greece
    "athens": "Greece",
    # Romania
    "bucharest": "Romania",
    # United States (cities often written without a state)
    "new york city": "United States", "chicago": "United States", "boston": "United States",
    "austin": "United States", "denver": "United States", "seattle": "United States",
    "san francisco": "United States", "los angeles": "United States", "atlanta": "United States",
    "miami": "United States", "dallas": "United States", "houston": "United States",
    "philadelphia": "United States", "milwaukee": "United States", "minneapolis": "United States",
    # Canada (cities)
    "toronto": "Canada", "vancouver": "Canada", "montreal": "Canada", "ottawa": "Canada",
    "calgary": "Canada", "edmonton": "Canada",
    # Australia / NZ
    "sydney": "Australia", "melbourne": "Australia", "brisbane": "Australia",
    "auckland": "New Zealand", "wellington": "New Zealand",
    # Asia / rest of world
    "singapore": "Singapore", "tokyo": "Japan", "seoul": "South Korea", "shanghai": "China",
    "hong kong": "China", "taipei": "Taiwan", "bangkok": "Thailand", "jakarta": "Indonesia",
    "mumbai": "India", "bangalore": "India", "delhi": "India", "dubai": "United Arab Emirates",
    "tel aviv": "Israel", "saigon": "Vietnam", "ho chi minh": "Vietnam",
}

_STATE_PROVINCES_SORTED = sorted(_STATE_PROVINCES, key=len, reverse=True)
_CITY_COUNTRIES_SORTED = sorted(_CITY_COUNTRIES, key=len, reverse=True)


def _match_country(s: str) -> str | None:
    low = s.lower()
    for name in _COUNTRY_NAMES_SORTED:
        if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", low):
            return _COUNTRY_NAMES[name]

    for region in _STATE_PROVINCES_SORTED:
        if re.search(rf"(?<![a-z]){re.escape(region)}(?![a-z])", low):
            return _STATE_PROVINCES[region]

    for city in _CITY_COUNTRIES_SORTED:
        if re.search(rf"(?<![a-z]){re.escape(city)}(?![a-z])", low):
            return _CITY_COUNTRIES[city]

    tokens = re.split(r"[,/()\-\u2013\u2014\s]+", low)
    for tok in tokens:
        if tok in _COUNTRY_CODES:
            return _COUNTRY_CODES[tok]
    return None


def detect_country(location: str | None, text: str | None = None) -> str | None:
    """Return the country from a location string.

    Only the location field is used — descriptions frequently mention many
    countries (company offices, global teams) and are not a reliable signal
    for where the job is actually located.
    """
    if location:
        return _match_country(location)
    return None


# ---------------------------------------------------------------------------
# Work mode (remote / hybrid / on-site)
# ---------------------------------------------------------------------------

def detect_work_mode(title: str | None = None, location: str | None = None,
                     description: str | None = None) -> str | None:
    """Return the work mode: "Remote", "Hybrid", or "On-site"."""
    combined = f"{title or ''}\n{location or ''}\n{description or ''}"
    t = combined.lower()

    if re.search(r"\bhybrid\b", t):
        return "Hybrid"

    has_remote = bool(re.search(
        r"\bremote(?:ly)?\b|\bwork from home\b|\bwfh\b|\bwork from anywhere\b|\btelecommut",
        t,
    ))
    has_onsite = bool(re.search(
        r"\bon[- ]?site\b|\bin[- ]?(?:the\s+)?office\b|\bin[- ]?person\b|\boffice[- ]?based\b",
        t,
    ))

    if has_remote and not has_onsite:
        return "Remote"
    if has_onsite and not has_remote:
        return "On-site"
    if has_remote:
        return "Remote"
    if has_onsite:
        return "On-site"
    return None


def classify_job(title: str | None = None, location: str | None = None,
                 full_description: str | None = None,
                 description: str | None = None) -> dict[str, str | None]:
    """Classify a job using all available text fields.

    Returns a dict with language_requirement, employment_type, country,
    and work_mode (each may be None when undetermined).
    """
    text = full_description or description or ""
    combined = f"{title or ''}\n{location or ''}\n{text}"
    return {
        "language_requirement": (
            detect_language_requirement(text) or detect_language_requirement(combined)
        ),
        "employment_type": (
            detect_employment_type(text) or detect_employment_type(combined)
        ),
        "country": detect_country(location, text),
        "work_mode": detect_work_mode(title, location, text),
    }


def is_remote_only_violation(
    country: str | None,
    work_mode: str | None,
    remote_only_countries: list[str],
) -> bool:
    """True if a job is in a remote-only country but the role is not remote.

    Used to keep remote-only countries (e.g. the US) to remote roles while
    allowing on-site jobs everywhere else.
    """
    if not remote_only_countries or not country:
        return False
    if country.lower() not in {c.lower() for c in remote_only_countries}:
        return False
    return (work_mode or "") != "Remote"


def backfill_classifications(conn: sqlite3.Connection) -> int:
    """Fill NULL classification columns for existing rows.

    Uses full_description when available, falling back to the short discovery
    description. Returns the number of rows updated.
    """
    rows = conn.execute(
        "SELECT url, title, location, full_description, description, "
        "language_requirement, employment_type, country, work_mode FROM jobs "
        "WHERE language_requirement IS NULL OR employment_type IS NULL "
        "OR country IS NULL OR work_mode IS NULL"
    ).fetchall()

    updated = 0
    for url, title, location, full_desc, desc, lang, emp, country, mode in rows:
        result = classify_job(title, location, full_desc, desc)
        current = {
            "language_requirement": lang,
            "employment_type": emp,
            "country": country,
            "work_mode": mode,
        }

        sets = []
        params = []
        for column in ("language_requirement", "employment_type", "country", "work_mode"):
            # Only fill columns that are still empty — never rewrite existing values.
            if current[column] is None and result[column] is not None:
                sets.append(f"{column} = ?")
                params.append(result[column])

        if sets:
            params.append(url)
            conn.execute(
                f"UPDATE jobs SET {', '.join(sets)} WHERE url = ?", params
            )
            updated += 1

    conn.commit()
    return updated


# ---------------------------------------------------------------------------
# LLM country resolution (for locations the regex can't parse)
# ---------------------------------------------------------------------------

_NON_COUNTRIES = {
    "remote", "remotely", "anywhere", "worldwide", "global", "various",
    "multiple", "unspecified", "not specified", "n/a", "na", "unknown",
    "none", "tbd", "remote work", "work from home", "fully remote",
    "remote only", "anywhere in the world", "remote country",
    "remote remote", "remote remote remote",
}

# Combined alias -> canonical country lookup for normalizing LLM output.
_COUNTRY_LOOKUP: dict[str, str] = {}
_COUNTRY_LOOKUP.update(_COUNTRY_NAMES)
_COUNTRY_LOOKUP.update(_COUNTRY_CODES)


def _is_remote_only(location: str) -> bool:
    """True for locations that are just 'Remote' / 'Anywhere' with no country."""
    normalized = re.sub(r"[\s\-_/().,;]+", " ", (location or "").lower()).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized in _NON_COUNTRIES


def _canonical_country(name: str | None) -> str | None:
    n = (name or "").strip()
    if not n:
        return None
    low = n.lower()
    if low in _NON_COUNTRIES:
        return None
    return _COUNTRY_LOOKUP.get(low, n)


# Values the LLM may return to mean "no additional language required".
_NON_LANGUAGES = {
    "none", "n/a", "na", "n a", "no", "null", "unknown", "not required",
    "no language", "no language required", "english", "english only",
    "english required", "english (only)", "only english", "none required",
}

_BILINGUAL_EN_FR = re.compile(
    r"bilingual|english\s*(?:and|&|/)\s*french|french\s*(?:and|&|/)\s*english",
    re.IGNORECASE,
)


def canonicalize_country(name: str | None) -> str | None:
    """Normalize an LLM-returned country name to the dashboard's canonical label."""
    return _canonical_country(name)


def canonicalize_language(name: str | None) -> str | None:
    """Normalize an LLM-returned language value; None when English-only/none."""
    n = (name or "").strip()
    if not n:
        return None
    low = n.lower()
    if low in _NON_LANGUAGES:
        return None
    if _BILINGUAL_EN_FR.search(low):
        return "Bilingual (EN/FR)"
    return _LANGUAGE_LABELS.get(low, n)


_NON_COMPANIES = {
    "unknown", "n/a", "na", "none", "not specified", "not stated",
    "unspecified", "confidential", "-", "--", "null",
}


def canonicalize_company(name: str | None) -> str | None:
    """Normalize an LLM-returned company name; None when unknown/empty."""
    n = (name or "").strip().strip("*_ ")
    if not n or n.lower() in _NON_COMPANIES:
        return None
    return n


# Prompt used by classify_jobs_with_llm to classify several jobs at once.
_JOB_CLASSIFY_PROMPT = """You classify job postings.

For EACH job below determine:
- country: the country where the job is located (English name, e.g. "Norway",
  "Italy", "United States"). Use the location and description. If the job is
  remote/unspecified or the country is unknown, use null.
- language: any language OTHER THAN ENGLISH that the job explicitly requires,
  lists as required/essential/fluent, or is an asset/advantage (e.g. "French").
  If it requires English and French, use "Bilingual (EN/FR)". If only English,
  use null.
- company: the hiring company's name (e.g. "Mastercard"). If the company is not
  stated anywhere, use null.

Return ONLY a JSON array with one object per job, in the same order, using the
given index. No explanation, no markdown.
Example: [{{"i": 0, "country": "Norway", "language": null, "company": "Acme AS"}}]

JOBS:
{jobs}"""


def classify_jobs_with_llm(
    conn: sqlite3.Connection,
    limit: int = 0,
    batch_size: int = 20,
) -> int:
    """Use the LLM to determine country + required language for jobs missing them.

    Only jobs whose country or language_requirement is still NULL are processed,
    so this is cheap to re-run — already-classified jobs are skipped. Jobs are
    batched into single requests to minimise API calls.

    Args:
        conn: Database connection.
        limit: Max jobs to process (0 = all pending).
        batch_size: Jobs per LLM request.

    Returns:
        Number of jobs updated.
    """
    query = (
        "SELECT url, title, location, description, full_description FROM jobs "
        "WHERE country IS NULL OR language_requirement IS NULL OR company IS NULL "
        "ORDER BY fit_score DESC NULLS LAST, discovered_at DESC"
    )
    if limit > 0:
        query += f" LIMIT {limit}"
    rows = conn.execute(query).fetchall()
    if not rows:
        return 0

    try:
        from applypilot.llm import get_client
        client = get_client("classify")
    except Exception:
        return 0  # No LLM provider configured — skip gracefully.

    from applypilot.discovery.smartextract import extract_json

    updated = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        payload = []
        for idx, row in enumerate(batch):
            _url, title, location, desc, full_desc = row
            text = (full_desc or desc or "")[:700]
            payload.append({
                "i": idx,
                "title": title or "",
                "location": location or "",
                "description": text,
            })

        prompt = _JOB_CLASSIFY_PROMPT.format(jobs=json.dumps(payload, ensure_ascii=False))
        try:
            raw = client.ask(prompt, temperature=0.0, max_tokens=4096)
            result = extract_json(raw)
        except Exception as e:
            log.warning("LLM job classification failed (batch %d): %s", start, e)
            continue

        if not isinstance(result, list):
            log.warning("LLM job classification returned non-list (batch %d)", start)
            continue

        by_index: dict[int, dict] = {}
        for item in result:
            if isinstance(item, dict) and isinstance(item.get("i"), int):
                by_index[item["i"]] = item

        for idx, row in enumerate(batch):
            url = row[0]
            item = by_index.get(idx)
            if not item:
                continue
            country = canonicalize_country(item.get("country"))
            language = canonicalize_language(item.get("language"))
            company = canonicalize_company(item.get("company"))
            sets, params = [], []
            if country:
                sets.append("country = ?")
                params.append(country)
            if language:
                sets.append("language_requirement = ?")
                params.append(language)
            if company:
                sets.append("company = ?")
                params.append(company)
            if sets:
                params.append(url)
                conn.execute(
                    f"UPDATE jobs SET {', '.join(sets)} WHERE url = ?", params
                )
                updated += 1
        conn.commit()

    return updated


def resolve_countries_with_llm(conn: sqlite3.Connection) -> int:
    """Resolve city-only/ambiguous locations to countries using a single LLM call.

    Runs after regex-based country detection. Collects all distinct locations
    that still have no country, sends them in one batched request, and writes
    the results back. Remote/Anywhere locations are skipped.

    Returns the number of locations resolved.
    """
    rows = conn.execute(
        "SELECT DISTINCT location FROM jobs "
        "WHERE country IS NULL AND location IS NOT NULL AND location != ''"
    ).fetchall()

    pending = [r[0] for r in rows if not _is_remote_only(r[0] or "")]
    if not pending:
        return 0

    try:
        from applypilot.llm import get_client
        client = get_client("classify")
    except Exception:
        return 0  # No LLM provider configured — skip gracefully.

    prompt = (
        "You are mapping job location strings to countries. "
        "Return a JSON object mapping each EXACT location string to its country "
        "(English name), or null if it is remote/unspecified/not a real place.\n\n"
        "Examples:\n"
        '  "Milan" -> "Italy"\n'
        '  "Illinois Remote Work" -> "United States"\n'
        '  "Edinburgh, UK (ZUK129)" -> "United Kingdom"\n'
        '  "Golcuk, TR" -> "Turkey"\n'
        '  "Remote" -> null\n\n'
        "Return ONLY valid JSON, no markdown, no explanation.\n\n"
        f"LOCATIONS: {json.dumps(pending)}"
    )

    try:
        from applypilot.discovery.smartextract import extract_json
        raw = client.ask(prompt, temperature=0.0, max_tokens=2048)
        mapping = extract_json(raw)
    except Exception as e:
        log.warning("LLM country resolution failed: %s", e)
        return 0

    if not isinstance(mapping, dict):
        return 0

    resolved = 0
    for location, country in mapping.items():
        canon = _canonical_country(country if isinstance(country, str) else None)
        if not canon or not isinstance(location, str):
            continue
        conn.execute(
            "UPDATE jobs SET country = ? WHERE location = ? AND country IS NULL",
            (canon, location),
        )
        resolved += 1

    conn.commit()
    return resolved


# Job-board site names (as stored in the `site` column). For those, `site` is
# the board, not the employer, so it must NOT be used as the company name.
_BOARD_SITES = {
    "linkedin", "indeed", "zip_recruiter", "ziprecruiter", "glassdoor",
    "simplyhired", "talent.com", "dice", "eluta", "randstad canada",
    "careerjet canada", "job bank canada", "builtin remote", "hacker news jobs",
    "remoteok", "weworkremotely", "remotive", "wellfound", "otta",
    "startup.jobs", "jobgether", "powertofly", "justremote", "himalayas",
    "nodesk", "working nomads", "remote.co", "flexjobs", "arc.dev",
    "jobspresso", "topstartups", "dynamitejobs", "4dayweek", "techstars jobs",
    "welcome to the jungle", "karrierestart", "nav arbeidsplassen", "jobbnorge",
    "google", "workopolis",
}


def backfill_company_from_site(conn: sqlite3.Connection) -> int:
    """For employer sites, copy `site` into `company` where company is missing.

    Board sites (linkedin, indeed, ...) are skipped because their `site` is the
    job board, not the employer. Cheap SQL only — no LLM.
    """
    placeholders = ",".join("?" for _ in _BOARD_SITES)
    cur = conn.execute(
        f"UPDATE jobs SET company = site WHERE company IS NULL "
        f"AND site IS NOT NULL AND trim(site) != '' "
        f"AND lower(site) NOT IN ({placeholders})",
        tuple(sorted(_BOARD_SITES)),
    )
    conn.commit()
    return cur.rowcount
