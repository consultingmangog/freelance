"""
scraper.py — Freelance Job Board scraper.

Fetches RSS feeds from a curated list of remote-tech job boards, plus (if
credentials are configured) the Adzuna JSON API for the French market, scores
each offer against the user's real experience (loaded from ``profile.yml``),
and stores results incrementally in ``missions.csv``.

Scoring is fully content-based: no title weighting. The title is only used as
part of the searchable text, not as a special signal. The full offer text
(title + summary + company) is matched against the profile's core_skills,
secondary_skills, domains, green_flags and red_flags.

Designed to run unattended in GitHub Actions. 100% RSS and REST APIs (no
Cloudflare-protected HTML endpoints) to keep CI IPs from being blocked.
"""

from __future__ import annotations

import csv
import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CSV_PATH = Path("missions.csv")
PROFILE_PATH = Path("profile.yml")

CSV_FIELDS = [
    "id",
    "date_scraped",
    "published",
    "title",
    "company",
    "source",
    "source_url",
    "url",
    "summary",
    "keywords",
    "matched_skills",
    "score",
    "match_pct",
    "match_grade",
]

USER_AGENT = "Mozilla/5.0 (compatible; JobBoardBot/1.0)"

# --- Sources (all verified to return valid RSS from CI IPs) ----------------

RSS_SOURCES: list[dict[str, str]] = [
    {
        "name": "Remotive",
        "url": "https://remotive.com/remote-jobs/feed",
        "homepage": "https://remotive.com",
    },
    {
        "name": "WeWorkRemotely",
        "url": "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "homepage": "https://weworkremotely.com",
    },
    {
        "name": "RemoteOK",
        "url": "https://remoteok.com/remote-dev-jobs.rss",
        "homepage": "https://remoteok.com",
    },
    {
        "name": "Himalayas",
        "url": "https://himalayas.app/jobs/rss?search=data+engineer",
        "homepage": "https://himalayas.app",
    },
    {
        "name": "Jobicy",
        "url": "https://jobicy.com/?feed=job_feed",
        "homepage": "https://jobicy.com",
    },
    {
        "name": "Codeur",
        "url": "https://www.codeur.com/projects.rss",
        "homepage": "https://www.codeur.com",
    },
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("scraper")


# ---------------------------------------------------------------------------
# Profile loading
# ---------------------------------------------------------------------------


@dataclass
class Profile:
    """Parsed view of profile.yml used for scoring."""

    remote_required: bool = True
    remote_terms: list[str] = field(default_factory=list)
    role_signals: list[str] = field(default_factory=list)
    core_skills: dict[str, int] = field(default_factory=dict)
    secondary_skills: dict[str, int] = field(default_factory=dict)
    domains: dict[str, int] = field(default_factory=dict)
    red_flags: list[str] = field(default_factory=list)
    green_flags: list[str] = field(default_factory=list)
    normalizer: int = 18
    min_score: int = 14
    grade_thresholds: dict[str, int] = field(
        default_factory=lambda: {"A": 80, "B": 50, "C": 30, "D": 0}
    )


def _weighted_dict(section: Any) -> dict[str, int]:
    """Accept either {key: {weight: n}} or {key: n} and return {key: n}."""
    if not section:
        return {}
    out: dict[str, int] = {}
    for key, value in section.items():
        if isinstance(value, dict):
            out[str(key).lower()] = int(value.get("weight", 1))
        else:
            out[str(key).lower()] = int(value)
    return out


def _flag_list(section: Any) -> list[str]:
    if not section:
        return []
    return [str(item).lower() for item in section]


def load_profile(path: Path) -> Profile:
    """Read profile.yml and return a Profile instance."""
    if not path.exists():
        logger.warning("profile.yml not found — scoring will be empty")
        return Profile()

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    scoring = data.get("scoring") or {}
    grades = scoring.get("grades") or {}
    return Profile(
        remote_required=bool(data.get("remote_required", True)),
        remote_terms=_flag_list(data.get("remote_terms")),
        role_signals=_flag_list(data.get("role_signals")),
        core_skills=_weighted_dict(data.get("core_skills")),
        secondary_skills=_weighted_dict(data.get("secondary_skills")),
        domains=_weighted_dict(data.get("domains")),
        red_flags=_flag_list(data.get("red_flags")),
        green_flags=_flag_list(data.get("green_flags")),
        normalizer=int(scoring.get("normalizer", 18)),
        min_score=int(scoring.get("min_score", 14)),
        grade_thresholds={
            "A": int(grades.get("A", 80)),
            "B": int(grades.get("B", 50)),
            "C": int(grades.get("C", 30)),
            "D": int(grades.get("D", 0)),
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class ScoreResult:
    score: int
    match_pct: int
    matched_skills: list[str]
    matched_domains: list[str]
    matched_flags: list[str]
    rejected: bool
    reject_reason: str = ""


def clean_text(raw: str | None) -> str:
    """Strip HTML tags and collapse whitespace."""
    if not raw:
        return ""
    soup = BeautifulSoup(raw, "html.parser")
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def mission_id(url: str, title: str) -> str:
    digest = hashlib.sha1(f"{url}|{title}".encode("utf-8")).hexdigest()
    return digest[:12]


def _strip_negations(text: str, negated_terms: list[str]) -> str:
    """Remove phrases like 'no remote' before substring-checking for 'remote'."""
    out = text
    for neg in negated_terms:
        out = out.replace(neg, "")
    return out


# Word-boundary match cache. A plain ``term in text`` check is too noisy for
# short terms: "r" would match in "senior", "hr" in "through", etc. Using
# \b boundaries filters these out while keeping multi-word matches like
# "data lake" or "data engineer" working correctly.
_pattern_cache: dict[str, re.Pattern] = {}


def _matches(term: str, text_lower: str) -> bool:
    pat = _pattern_cache.get(term)
    if pat is None:
        pat = re.compile(r"\b" + re.escape(term) + r"\b")
        _pattern_cache[term] = pat
    return pat.search(text_lower) is not None


def score_offer(
    title: str,
    summary: str,
    company: str,
    profile: Profile,
    red_flag_text: str | None = None,
) -> ScoreResult:
    """Score an offer purely on content match against the user profile.

    The scoring is symmetric across title/summary/company — the title has no
    special weight. What matters is which skills, domains and flags from the
    profile appear in the full text.

    ``red_flag_text`` lets the caller pass a *different* (usually shorter)
    text for red-flag checks. This is used during hydration: the red flags
    are checked against the condensed RSS title+summary (which carries the
    essential role requirements), while positive scoring runs on the full
    hydrated page. Without this distinction, every full-page dump would hit
    a stray "aws" in a footer and get rejected.
    """
    body_low = f"{title} {summary} {company}".lower()
    red_flag_body = (red_flag_text or f"{title} {summary}").lower()

    # --- Role signal filter -------------------------------------------------
    # Require at least one data-specific role signal in the offer text.
    # This prevents non-data roles (Middleware, Backend, etc.) from passing
    # simply because they mention Python/SQL/Oracle that the user also knows.
    if profile.role_signals and not any(
        _matches(sig, body_low) for sig in profile.role_signals
    ):
        return ScoreResult(
            score=0,
            match_pct=0,
            matched_skills=[],
            matched_domains=[],
            matched_flags=[],
            rejected=True,
            reject_reason="no-role-signal",
        )

    # --- Hard rejection: red flags from profile -----------------------------
    for flag in profile.red_flags:
        if _matches(flag, red_flag_body):
            return ScoreResult(
                score=0,
                match_pct=0,
                matched_skills=[],
                matched_domains=[],
                matched_flags=[],
                rejected=True,
                reject_reason=f"red-flag:{flag}",
            )

    # --- Mandatory remote check --------------------------------------------
    # The user requires explicit remote/hybrid mention in every offer. An
    # offer that's silent about remote is rejected even if it scores high
    # otherwise. Strip negations like "no remote" first so they don't
    # falsely satisfy the check.
    if profile.remote_required and profile.remote_terms:
        body_for_remote = _strip_negations(
            body_low,
            ["no remote", "no-remote", "not remote", "sans remote", "no télétravail"],
        )
        if not any(_matches(term, body_for_remote) for term in profile.remote_terms):
            return ScoreResult(
                score=0,
                match_pct=0,
                matched_skills=[],
                matched_domains=[],
                matched_flags=[],
                rejected=True,
                reject_reason="no-remote-mention",
            )

    # --- Positive scoring ---------------------------------------------------
    score = 0
    matched_skills: list[str] = []
    matched_domains: list[str] = []
    matched_flags: list[str] = []

    # Core skills (weighted)
    for skill, weight in profile.core_skills.items():
        if _matches(skill, body_low):
            matched_skills.append(skill)
            score += weight

    # Secondary skills
    for skill, weight in profile.secondary_skills.items():
        if _matches(skill, body_low):
            matched_skills.append(skill)
            score += weight

    # Domains (weighted, typically +2)
    for domain, weight in profile.domains.items():
        if _matches(domain, body_low):
            matched_domains.append(domain)
            score += weight

    # Green flags (+1 each). Strip explicit negations like "no remote" before
    # checking the remote group.
    body_for_flags = _strip_negations(
        body_low, ["no remote", "no-remote", "not remote", "sans remote"]
    )
    for flag in profile.green_flags:
        if _matches(flag, body_for_flags):
            matched_flags.append(flag)
            score += 1

    # Normalize score → match_pct
    normalizer = max(profile.normalizer, 1)
    match_pct = min(100, round(score * 100 / normalizer))

    return ScoreResult(
        score=score,
        match_pct=match_pct,
        matched_skills=matched_skills,
        matched_domains=matched_domains,
        matched_flags=matched_flags,
        rejected=False,
    )


def grade_from_pct(pct: int, thresholds: dict[str, int]) -> str:
    if pct >= thresholds["A"]:
        return "A"
    if pct >= thresholds["B"]:
        return "B"
    if pct >= thresholds["C"]:
        return "C"
    return "D"


# ---------------------------------------------------------------------------
# Feed fetching
# ---------------------------------------------------------------------------


def fetch_feed(url: str) -> list:
    """Download an RSS feed and return its entries (empty list on failure)."""
    logger.info("Fetching %s", url)
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, */*"},
            timeout=20,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return []

    parsed = feedparser.parse(response.content)
    if parsed.bozo and not parsed.entries:
        logger.warning("Malformed feed from %s: %s", url, parsed.bozo_exception)
        return []
    return list(parsed.entries)


# ---------------------------------------------------------------------------
# Adzuna source (JSON API) — covers the French market via their aggregation
# of Pôle Emploi / Indeed FR / Monster FR / Reed / StepStone / etc.
# Free tier: 250 requests/day. Credentials live in env vars.
# ---------------------------------------------------------------------------

ADZUNA_BASE_URL = "https://api.adzuna.com/v1/api/jobs"

# Keyword queries used to pull data-engineering offers from Adzuna. Each
# query is one API request; keep the list tight to stay well under the
# free-tier budget (250 req/day).
ADZUNA_QUERIES = [
    "data engineer",
    "data lake",
    "analytics engineer",
    "hadoop",
]


def fetch_adzuna_offers(country: str = "fr") -> list[dict]:
    """Query the Adzuna API for jobs matching the profile's role signals.

    Returns a list of pseudo-RSS-entries (dicts with the same keys feedparser
    exposes: ``title``, ``link``, ``summary``, ``author``, ``published``).
    Skips gracefully if credentials are missing.
    """
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        logger.info("Adzuna credentials not set — skipping (set env vars to enable)")
        return []

    out: list[dict] = []
    for query in ADZUNA_QUERIES:
        url = f"{ADZUNA_BASE_URL}/{country}/search/1"
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "what": query,
            "results_per_page": 50,
            "max_days_old": 30,
            "content-type": "application/json",
        }
        logger.info("Fetching Adzuna %s: %s", country, query)
        try:
            response = requests.get(url, params=params, timeout=20)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            logger.warning("Adzuna query failed (%s): %s", query, exc)
            continue
        except ValueError as exc:  # JSON decode
            logger.warning("Adzuna non-JSON response for %s: %s", query, exc)
            continue

        results = data.get("results") or []
        logger.info("  → %d results from Adzuna (%s)", len(results), query)

        for item in results:
            company = (item.get("company") or {}).get("display_name") or ""
            location = (item.get("location") or {}).get("display_name") or ""
            summary = item.get("description") or ""
            if location and location.lower() not in summary.lower():
                # Surface the location in the summary so the geography
                # context is visible in the dashboard and the scoring.
                summary = f"{summary} — {location}"

            out.append(
                {
                    "title": item.get("title") or "",
                    "link": item.get("redirect_url") or "",
                    "summary": summary,
                    "author": company,
                    "published": item.get("created") or "",
                }
            )

    return out


def fetch_page_text(url: str, timeout: int = 10) -> str:
    """Download the full HTML page of an offer and extract visible text.

    Returns empty string on any failure — the caller falls back to the
    original RSS summary. Used to enrich short RSS excerpts so that the
    profile-based content scoring has enough signal to work with.
    """
    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=timeout,
            allow_redirects=True,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.debug("Hydration failed for %s: %s", url, exc)
        return ""

    soup = BeautifulSoup(response.content, "html.parser")
    # Drop non-content elements.
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    # Cap hydration length to avoid passing huge blobs through the scoring loop.
    return text[:5000]


def normalize_entry(
    entry,
    source_name: str,
    source_homepage: str,
    profile: Profile,
    hydrate: bool = True,
) -> dict | None:
    """Turn a feedparser entry into a CSV row dict, or None if it should be dropped.

    If ``hydrate`` is True, offers that pass the cheap title-based role filter
    are re-scored against the full page HTML (not just the RSS summary).
    """
    title = clean_text(entry.get("title"))
    url = entry.get("link") or ""
    if not title or not url:
        return None

    summary = clean_text(entry.get("summary") or entry.get("description") or "")
    company = clean_text(entry.get("author") or "") or source_name

    # Cheap pre-filter: the RSS title+summary must contain a role signal.
    # If it doesn't, the offer isn't data-related and we drop it immediately
    # without wasting an HTTP fetch on hydration.
    cheap_text = f"{title} {summary} {company}".lower()
    has_role_signal_cheap = any(
        _matches(sig, cheap_text) for sig in profile.role_signals
    )
    if not has_role_signal_cheap:
        return None

    # Hydrate with full page HTML so the scoring has rich signal (RSS
    # summaries are often truncated to 150-200 chars, which is too thin for
    # profile-based content matching to score confidently).
    if hydrate:
        full_text = fetch_page_text(url)
        scored_summary = full_text if full_text else summary
    else:
        scored_summary = summary

    # Red flags checked on the condensed title+summary only — the hydrated
    # full page is usually too noisy (footers, related-jobs, etc.).
    result = score_offer(
        title,
        scored_summary,
        company,
        profile,
        red_flag_text=f"{title} {summary}",
    )
    if result.rejected:
        logger.debug("Rejected '%s' (%s)", title[:60], result.reject_reason)
        return None
    if result.score < profile.min_score:
        return None

    grade = grade_from_pct(result.match_pct, profile.grade_thresholds)

    published = ""
    for key in ("published", "updated", "pubDate"):
        if entry.get(key):
            published = str(entry.get(key))
            break

    # Pack keywords/skills/domains/flags into readable columns.
    all_matched = result.matched_skills + result.matched_domains
    keyword_label = ", ".join(all_matched + result.matched_flags)

    return {
        "id": mission_id(url, title),
        "date_scraped": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "published": published,
        "title": title,
        "company": company,
        "source": source_name,
        "source_url": source_homepage,
        "url": url,
        "summary": summary[:500],
        "keywords": keyword_label,
        "matched_skills": ", ".join(all_matched),
        "score": result.score,
        "match_pct": result.match_pct,
        "match_grade": grade,
    }


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------


def load_existing(path: Path, min_score: int) -> dict[str, dict]:
    """Load previously scraped rows, dropping any that no longer meet the
    current ``min_score`` threshold. This lets profile/threshold changes take
    effect naturally on the next run (old sub-threshold rows get purged)."""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        kept: dict[str, dict] = {}
        for row in reader:
            if not row.get("id"):
                continue
            try:
                score = int(row.get("score") or 0)
            except ValueError:
                score = 0
            if score < min_score:
                continue
            kept[row["id"]] = row
    return kept


def save(path: Path, rows: Iterable[dict]) -> None:
    def sort_key(row: dict) -> tuple:
        return (
            -int(row.get("match_pct") or 0),
            row.get("date_scraped") or "",
        )

    sorted_rows = sorted(rows, key=sort_key)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in sorted_rows:
            writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    profile = load_profile(PROFILE_PATH)
    logger.info(
        "Loaded profile: %d core / %d secondary / %d domains / %d red / %d green "
        "(min_score=%d, normalizer=%d)",
        len(profile.core_skills),
        len(profile.secondary_skills),
        len(profile.domains),
        len(profile.red_flags),
        len(profile.green_flags),
        profile.min_score,
        profile.normalizer,
    )

    existing = load_existing(CSV_PATH, profile.min_score)
    logger.info("Loaded %d existing missions (above threshold)", len(existing))

    new_count = 0

    # RSS sources
    for source in RSS_SOURCES:
        entries = fetch_feed(source["url"])
        logger.info("  → %d entries from %s", len(entries), source["name"])

        for entry in entries:
            row = normalize_entry(
                entry, source["name"], source["homepage"], profile
            )
            if row is None:
                continue
            if row["id"] in existing:
                continue
            existing[row["id"]] = row
            new_count += 1

    # Adzuna (JSON API) — covers the FR market (Pôle Emploi, Indeed FR, ...).
    # Skips silently if ADZUNA_APP_ID / ADZUNA_APP_KEY env vars aren't set.
    adzuna_entries = fetch_adzuna_offers(country="fr")
    if adzuna_entries:
        logger.info("  → %d total results from Adzuna FR", len(adzuna_entries))
        for entry in adzuna_entries:
            row = normalize_entry(
                entry, "Adzuna FR", "https://www.adzuna.fr", profile
            )
            if row is None:
                continue
            if row["id"] in existing:
                continue
            existing[row["id"]] = row
            new_count += 1

    save(CSV_PATH, existing.values())
    logger.info(
        "Saved %d missions to %s (%d new this run)",
        len(existing),
        CSV_PATH,
        new_count,
    )


if __name__ == "__main__":
    main()
