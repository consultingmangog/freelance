"""
app.py — Streamlit dashboard for the Freelance Job Board.

Reads ``missions.csv`` produced by ``scraper.py`` and exposes an interactive
table sorted by match percentage, with KPIs, sidebar filters, and a manual
"run scraper now" action wired to the GitHub Actions workflow_dispatch API.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

CSV_PATH = Path("missions.csv")
WORKFLOW_FILE = "scrape.yml"
DEFAULT_BRANCH = "claude/job-board-automation-yTvBX"

st.set_page_config(
    page_title="Freelance Job Board",
    page_icon="💼",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


@st.cache_data(ttl=300)
def load_data(path: Path) -> pd.DataFrame:
    columns = [
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

    if not path.exists():
        return pd.DataFrame(columns=columns)

    df = pd.read_csv(path, dtype=str).fillna("")
    for col in columns:
        if col not in df.columns:
            df[col] = ""

    df["date_scraped_dt"] = pd.to_datetime(df["date_scraped"], errors="coerce").dt.date
    df["score"] = pd.to_numeric(df["score"], errors="coerce").fillna(0).astype(int)
    df["match_pct"] = pd.to_numeric(df["match_pct"], errors="coerce").fillna(0).astype(int)
    return df


# ---------------------------------------------------------------------------
# Manual scrape trigger (GitHub API workflow_dispatch)
# ---------------------------------------------------------------------------


def trigger_workflow_dispatch() -> tuple[bool, str]:
    """Trigger the Daily Scrape workflow via the GitHub API.

    Reads credentials from ``st.secrets``:
      - ``GITHUB_TOKEN`` : a PAT with ``workflow`` scope
      - ``GITHUB_REPO``  : ``owner/repo`` (e.g. ``consultingmangog/freelance``)
      - ``GITHUB_BRANCH``: optional branch ref (defaults to the main branch)
    """
    try:
        token = st.secrets["GITHUB_TOKEN"]
        repo = st.secrets["GITHUB_REPO"]
    except (KeyError, FileNotFoundError):
        return (
            False,
            "Secrets `GITHUB_TOKEN` et `GITHUB_REPO` non configurés "
            "(voir README → Trigger manuel depuis Streamlit).",
        )

    branch = st.secrets.get("GITHUB_BRANCH", DEFAULT_BRANCH)
    api_url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/"
        f"{WORKFLOW_FILE}/dispatches"
    )

    try:
        response = requests.post(
            api_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"ref": branch},
            timeout=15,
        )
    except requests.RequestException as exc:
        return False, f"Erreur réseau : {exc}"

    if response.status_code == 204:
        return True, f"Workflow déclenché sur `{branch}`. Attends ~1 min puis rafraîchis."
    return False, f"GitHub API a répondu {response.status_code}: {response.text[:200]}"


df = load_data(CSV_PATH)

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.title("Freelance Job Board — Data Engineer / Remote")
st.caption(
    "Auto-updated daily via GitHub Actions. Sources: Remotive, WeWorkRemotely, "
    "RemoteOK, Himalayas, Jobicy, Codeur."
)

# ---------------------------------------------------------------------------
# KPIs
# ---------------------------------------------------------------------------

today = date.today()

kpi_cols = st.columns(5)

total = len(df)
new_today = int((df["date_scraped_dt"] == today).sum()) if not df.empty else 0
grade_a = int((df["match_grade"] == "A").sum()) if not df.empty else 0
grade_b = int((df["match_grade"] == "B").sum()) if not df.empty else 0
avg_pct = int(df["match_pct"].mean()) if not df.empty else 0

kpi_cols[0].metric("Total missions", total)
kpi_cols[1].metric("Nouvelles aujourd'hui", new_today)
kpi_cols[2].metric("Grade A (forte chance)", grade_a)
kpi_cols[3].metric("Grade B (bonne chance)", grade_b)
kpi_cols[4].metric("Score moyen", f"{avg_pct}%")

st.divider()

# ---------------------------------------------------------------------------
# Sidebar — Actions + Filters
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("Actions")

    col_a, col_b = st.columns(2)
    if col_a.button("⟳ Rafraîchir", use_container_width=True, help="Vide le cache et relit missions.csv"):
        st.cache_data.clear()
        st.rerun()

    if col_b.button("▶ Scraper", use_container_width=True, help="Déclenche le workflow GitHub Actions"):
        ok, msg = trigger_workflow_dispatch()
        if ok:
            st.success(msg)
        else:
            st.warning(msg)

    st.divider()
    st.header("Filtres")

    search = st.text_input("Recherche (titre, société, résumé)", "")

    if not df.empty:
        sources_available = sorted(s for s in df["source"].unique() if s)
        selected_sources = st.multiselect(
            "Source",
            sources_available,
            default=sources_available,
        )

        grades_available = ["A", "B", "C", "D"]
        selected_grades = st.multiselect(
            "Grade (chance d'être retenu)",
            grades_available,
            default=grades_available,
        )

        valid_dates = df["date_scraped_dt"].dropna()
        if not valid_dates.empty:
            min_date = valid_dates.min()
            max_date = valid_dates.max()
        else:
            min_date = max_date = today
        date_range = st.date_input(
            "Date de scrape",
            value=(min_date, max_date),
            min_value=min_date,
            max_value=max_date,
        )
    else:
        selected_sources = []
        selected_grades = ["A", "B", "C", "D"]
        date_range = (today, today)

# ---------------------------------------------------------------------------
# Apply filters
# ---------------------------------------------------------------------------

filtered = df.copy()

if not filtered.empty:
    if search:
        mask = (
            filtered["title"].str.contains(search, case=False, na=False)
            | filtered["company"].str.contains(search, case=False, na=False)
            | filtered["summary"].str.contains(search, case=False, na=False)
        )
        filtered = filtered[mask]

    if selected_sources:
        filtered = filtered[filtered["source"].isin(selected_sources)]

    if selected_grades:
        filtered = filtered[filtered["match_grade"].isin(selected_grades)]

    if isinstance(date_range, tuple) and len(date_range) == 2:
        start, end = date_range
        mask_dates = filtered["date_scraped_dt"].between(start, end)
        filtered = filtered[mask_dates]

    filtered = filtered.sort_values(
        by=["match_pct", "date_scraped"], ascending=[False, False]
    )

# ---------------------------------------------------------------------------
# Results table
# ---------------------------------------------------------------------------

st.subheader(f"Offres ({len(filtered)})")

if filtered.empty:
    st.info(
        "Aucune offre pour l'instant. Le scraper tourne quotidiennement à 07:00 UTC "
        "via GitHub Actions — les résultats apparaîtront après la première exécution. "
        "Tu peux aussi cliquer sur ▶ Scraper dans la sidebar pour lancer un run manuel."
    )
else:
    display = filtered[
        [
            "match_pct",
            "match_grade",
            "title",
            "company",
            "source",
            "source_url",
            "matched_skills",
            "date_scraped",
            "url",
        ]
    ].rename(
        columns={
            "match_pct": "Chances %",
            "match_grade": "Grade",
            "title": "Titre",
            "company": "Société",
            "source": "Source",
            "source_url": "Site source",
            "matched_skills": "Skills matchés",
            "date_scraped": "Date scrape",
            "url": "Lien de l'offre",
        }
    )

    st.dataframe(
        display,
        hide_index=True,
        use_container_width=True,
        height=640,
        column_config={
            "Chances %": st.column_config.ProgressColumn(
                "Chances d'être retenu",
                help="Score de matching expérience (0-100%)",
                format="%d%%",
                min_value=0,
                max_value=100,
            ),
            "Grade": st.column_config.TextColumn(
                "Grade",
                help="A = forte chance, B = bonne, C = faible, D = peu de chance",
                width="small",
            ),
            "Titre": st.column_config.TextColumn("Titre", width="large"),
            "Société": st.column_config.TextColumn("Société", width="medium"),
            "Source": st.column_config.TextColumn("Source", width="small"),
            "Site source": st.column_config.LinkColumn(
                "Site source",
                help="Homepage de la plateforme d'origine",
                display_text="Site ↗",
                width="small",
            ),
            "Skills matchés": st.column_config.TextColumn(
                "Skills matchés", width="medium"
            ),
            "Date scrape": st.column_config.TextColumn("Date", width="small"),
            "Lien de l'offre": st.column_config.LinkColumn(
                "Lien de l'offre",
                help="URL directe vers l'offre sur la plateforme source",
                width="large",
            ),
        },
    )
