"""
scraper.py — Freelance Job Board scraper.

Fetches RSS feeds from a curated list of remote-tech job boards, filters offers
by inclusion keywords, computes an experience-match score, and stores results
incrementally in ``missions.csv``.

Designed to run unattended in GitHub Actions. 100% RSS (no Cloudflare-protected
HTML endpoints) to keep CI IPs from being blocked.
"""

from __future__ import annotations

import csv
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import feedparser
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CSV_PATH = Path("missions.csv")

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

# --- Filters ---------------------------------------------------------------

# Stage 1 — inclusion keywords (an offer must match at least one to be
# considered). Intentionally broad; the scoring step refines further.
INCLUSION_KEYWORDS = [
    "data engineer",
    "analytics engineer",
    "gcp",
    "google cloud",
    "bigquery",
    "hive",
    "hadoop",
    "python",
    "remote",
    "dbt",
    "snowflake",
    "airflow",
    "data warehouse",
    "etl",
]

# Stage 2 — scoring skills (each unique occurrence adds +2 to score).
CORE_SKILLS = [
    "dbt",
    "gcp",
    "google cloud",
    "bigquery",
    "hive",
    "hadoop",
    "snowflake",
    "redshift",
    "data warehouse",
    "dwh",
    "airflow",
]

REMOTE_TERMS = ["remote", "télétravail", "teletravail", "fully remote", "100% remote"]
SENIORITY_TERMS = ["senior", "lead", "confirmé", "confirme", "principal", "staff"]
ONSITE_TERMS = [
    "on-site",
    "on site",
    "onsite",
    "présentiel",
    "presentiel",
    "sur site",
    "no remote",
    "no-remote",
]

MIN_SCORE_TO_KEEP = 1

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("scraper")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class ScoreResult:
    score: int
    matched_skills: list[str]
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


def find_inclusion_keywords(text: str) -> list[str]:
    low = text.lower()
    return [kw for kw in INCLUSION_KEYWORDS if kw in low]


def score_offer(title: str, summary: str, company: str) -> ScoreResult:
    """Compute match score and rejection status for an offer."""
    title_low = title.lower()
    body_low = f"{title} {summary} {company}".lower()

    # Hard rejection rules
    has_gcp = any(k in body_low for k in ("gcp", "google cloud", "bigquery"))
    has_aws = "aws" in body_low or "amazon web services" in body_low
    has_azure = "azure" in body_low
    has_onsite = any(k in body_low for k in ONSITE_TERMS)

    # Check remote AFTER stripping negated forms ("no remote", "not remote"),
    # otherwise naive substring matching flips has_remote to True when the
    # offer explicitly says "no remote".
    body_for_remote = body_low
    for negation in ("no remote", "no-remote", "not remote", "sans remote"):
        body_for_remote = body_for_remote.replace(negation, "")
    has_remote = any(k in body_for_remote for k in REMOTE_TERMS)

    if has_aws and not has_gcp:
        return ScoreResult(0, [], rejected=True, reject_reason="aws-only")
    if has_azure and not has_gcp:
        return ScoreResult(0, [], rejected=True, reject_reason="azure-only")
    if has_onsite and not has_remote:
        return ScoreResult(0, [], rejected=True, reject_reason="onsite-only")

    score = 0

    # Role match (primary signal — title weighted more)
    if "data engineer" in title_low:
        score += 3
    elif "analytics engineer" in title_low:
        score += 2
    elif "data" in title_low and any(
        k in title_low for k in ("engineer", "pipeline", "platform")
    ):
        score += 1

    # Core skills
    matched_skills: list[str] = []
    for skill in CORE_SKILLS:
        if skill in body_low and skill not in matched_skills:
            matched_skills.append(skill)
            score += 2

    # Remote bonus
    if has_remote:
        score += 2

    # Seniority bonus
    if any(term in body_low for term in SENIORITY_TERMS):
        score += 1

    return ScoreResult(score=score, matched_skills=matched_skills, rejected=False)


def grade_from_pct(pct: int) -> str:
    if pct >= 80:
        return "A"
    if pct >= 50:
        return "B"
    if pct >= 30:
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


def normalize_entry(entry, source_name: str, source_homepage: str) -> dict | None:
    """Turn a feedparser entry into a CSV row dict, or None if it should be dropped."""
    title = clean_text(entry.get("title"))
    url = entry.get("link") or ""
    if not title or not url:
        return None

    summary = clean_text(entry.get("summary") or entry.get("description") or "")
    company = clean_text(entry.get("author") or "") or source_name

    combined = f"{title} {summary} {company}"
    matched_keywords = find_inclusion_keywords(combined)
    if not matched_keywords:
        return None

    result = score_offer(title, summary, company)
    if result.rejected:
        logger.debug("Rejected '%s' (%s)", title[:60], result.reject_reason)
        return None
    if result.score < MIN_SCORE_TO_KEEP:
        return None

    match_pct = min(100, round(result.score * 10))
    grade = grade_from_pct(match_pct)

    published = ""
    for key in ("published", "updated", "pubDate"):
        if entry.get(key):
            published = str(entry.get(key))
            break

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
        "keywords": ", ".join(matched_keywords),
        "matched_skills": ", ".join(result.matched_skills),
        "score": result.score,
        "match_pct": match_pct,
        "match_grade": grade,
    }


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------


def load_existing(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return {row["id"]: row for row in reader if row.get("id")}


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
    existing = load_existing(CSV_PATH)
    logger.info("Loaded %d existing missions", len(existing))

    new_count = 0
    for source in RSS_SOURCES:
        entries = fetch_feed(source["url"])
        logger.info("  → %d entries from %s", len(entries), source["name"])

        for entry in entries:
            row = normalize_entry(entry, source["name"], source["homepage"])
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
