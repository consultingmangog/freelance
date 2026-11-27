"""
app.py — Streamlit dashboard for the Freelance Job Board.

Reads ``missions.csv`` produced by ``scraper.py`` and exposes an interactive
table sorted by match percentage, with KPIs, sidebar filters, a manual
"run scraper now" action wired to the GitHub Actions workflow_dispatch API,
and an insights panel that surfaces top in-demand skills, gap skills (not
in the user's profile) and certifications mentioned across the offers.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
import yaml

CSV_PATH = Path("missions.csv")
PROFILE_PATH = Path("profile.yml")
WORKFLOW_FILE = "scrape.yml"
DEFAULT_BRANCH = "claude/job-board-automation-yTvBX"

st.set_page_config(
    page_title="Freelance Job Board",
    page_icon="💼",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# Skill analysis config — GAP skills (not in user's profile) and
# CERTIFICATIONS. Detected via word-boundary regex against the full
# offer summary. Tweak freely; the user's actual profile lives in
# profile.yml and is subtracted out of the gap list at runtime.
# ---------------------------------------------------------------------------

GAP_TECH = [
    ("Spark",         [r"\bspark\b", r"\bpyspark\b"]),
    ("Databricks",    [r"\bdatabricks\b"]),
    ("Kafka",         [r"\bkafka\b", r"\bconfluent\b"]),
    ("Snowflake",     [r"\bsnowflake\b"]),
    ("dbt",           [r"\bdbt\b"]),
    ("Airflow",       [r"\bairflow\b"]),
    ("Dagster",       [r"\bdagster\b"]),
    ("Kubernetes",    [r"\bkubernetes\b", r"\bk8s\b"]),
    ("Docker",        [r"\bdocker\b"]),
    ("Terraform",     [r"\bterraform\b"]),
    ("Scala",         [r"\bscala\b"]),
    ("Java",          [r"\bjava\b"]),
    ("Elasticsearch", [r"\belasticsearch\b", r"\belastic search\b"]),
    ("MongoDB",       [r"\bmongodb\b"]),
    ("Cassandra",     [r"\bcassandra\b"]),
    ("PostgreSQL",    [r"\bpostgres(ql)?\b"]),
    ("ClickHouse",    [r"\bclickhouse\b"]),
    ("Flink",         [r"\bflink\b"]),
    ("Iceberg",       [r"\bapache iceberg\b", r"\biceberg\b"]),
    ("Delta Lake",    [r"\bdelta lake\b", r"\bdelta-lake\b"]),
    ("Power BI",      [r"\bpower\s*bi\b"]),
    ("Tableau",       [r"\btableau\b"]),
    ("Looker",        [r"\blooker\b"]),
    ("Databricks SQL",[r"\bdatabricks sql\b"]),
    ("Rust",          [r"\brust\b"]),
    ("Go",            [r"\bgolang\b"]),
    ("TypeScript",    [r"\btypescript\b"]),
]

CERTIFICATIONS = [
    ("GCP Professional Data Engineer",
        [r"gcp professional data engineer",
         r"google cloud professional data engineer",
         r"professional data engineer"]),
    ("GCP Professional Cloud Architect",
        [r"gcp professional cloud architect",
         r"professional cloud architect"]),
    ("GCP Associate Cloud Engineer",
        [r"gcp associate cloud engineer",
         r"associate cloud engineer"]),
    ("GCP Professional ML Engineer",
        [r"professional machine learning engineer",
         r"professional ml engineer"]),
    ("Databricks Certified Data Engineer",
        [r"databricks certified.*data engineer",
         r"databricks.*data engineer.*associate",
         r"databricks.*data engineer.*professional"]),
    ("Cloudera Certified",
        [r"cloudera certified",
         r"\bcca\b.*cloudera",
         r"ccp data engineer",
         r"cloudera data.*certification"]),
    ("Snowflake SnowPro",
        [r"\bsnowpro\b", r"snowflake certif"]),
    ("dbt Analytics Engineering",
        [r"dbt analytics engineering certification",
         r"dbt certified"]),
    ("Confluent Kafka Developer",
        [r"confluent certified",
         r"certified (?:kafka )?developer.*confluent",
         r"ccdak\b"]),
    ("Terraform Associate (HashiCorp)",
        [r"terraform associate",
         r"hashicorp certified.*terraform"]),
    ("Kubernetes CKA / CKAD",
        [r"\bcka\b", r"\bckad\b", r"certified kubernetes"]),
    ("Azure DP-203 Data Engineer",
        [r"\bdp-?203\b", r"azure data engineer associate"]),
    ("Apache Spark",
        [r"spark.*certif", r"certified spark"]),
]


@st.cache_data(ttl=300)
def load_profile_skills(path: Path) -> set[str]:
    """Return the lowercased set of skill terms declared in profile.yml
    (core + secondary). Used to subtract the user's own skills from the
    gap-tech analysis so we only highlight real gaps."""
    if not path.exists():
        return set()
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    out: set[str] = set()
    for section in ("core_skills", "secondary_skills"):
        entries = data.get(section) or {}
        for key in entries.keys():
            out.add(str(key).lower())
    return out


def count_patterns(
    texts: list[str], items: list[tuple[str, list[str]]]
) -> list[tuple[str, int]]:
    """For each (display_name, [regex_patterns]), count how many texts
    match at least one pattern. Returns sorted (desc) list of (name, count).
    """
    compiled = [
        (name, [re.compile(p, re.IGNORECASE) for p in patterns])
        for name, patterns in items
    ]
    counts: Counter = Counter()
    for t in texts:
        for name, regexes in compiled:
            if any(rx.search(t) for rx in regexes):
                counts[name] += 1
    return sorted(counts.items(), key=lambda kv: -kv[1])


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
    "RemoteOK, Himalayas, Jobicy, Codeur, Adzuna FR, Free-Work."
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

# ---------------------------------------------------------------------------
# Skills & certifications insights (collapsible)
# ---------------------------------------------------------------------------

if not df.empty:
    profile_skills = load_profile_skills(PROFILE_PATH)
    summaries = df["summary"].fillna("").astype(str).tolist()
    n_offers = len(df)

    # (1) Skills from the profile that show up the most. We use the
    # matched_skills column which already carries the intersection
    # profile ∩ offer — just need to count them across rows.
    profile_counter: Counter = Counter()
    for raw in df["matched_skills"].fillna("").astype(str):
        for token in (t.strip().lower() for t in raw.split(",") if t.strip()):
            profile_counter[token] += 1
    top_profile = profile_counter.most_common(15)

    # (2) Gap skills — subtract the ones the user already declared in
    # profile.yml so we only suggest real gaps.
    gap_items_filtered = [
        (name, patterns) for name, patterns in GAP_TECH
        if name.lower() not in profile_skills
    ]
    gap_counts = count_patterns(summaries, gap_items_filtered)[:12]

    # (3) Certifications mentioned in offer text.
    cert_counts = count_patterns(summaries, CERTIFICATIONS)[:12]

    with st.expander(
        "📊 Analyse compétences & certifications demandées",
        expanded=False,
    ):
        st.caption(
            f"Agrégation sur les {n_offers} offres retenues. "
            "Ajuste les listes dans `app.py` (GAP_TECH, CERTIFICATIONS) "
            "ou dans `profile.yml` pour les skills de référence."
        )

        col_a, col_b, col_c = st.columns(3)

        with col_a:
            st.markdown("**💪 Tes skills en demande**")
            st.caption("Compétences de ton profil qui reviennent le plus.")
            if top_profile:
                profile_df = pd.DataFrame(
                    [
                        {
                            "Skill": skill,
                            "Offres": count,
                            "%": round(100 * count / n_offers),
                        }
                        for skill, count in top_profile
                    ]
                )
                st.dataframe(
                    profile_df,
                    hide_index=True,
                    use_container_width=True,
                    height=min(35 * (len(profile_df) + 1) + 3, 440),
                    column_config={
                        "Offres": st.column_config.ProgressColumn(
                            "Offres",
                            format="%d",
                            min_value=0,
                            max_value=max(count for _, count in top_profile),
                        ),
                        "%": st.column_config.NumberColumn("%", format="%d %%"),
                    },
                )
            else:
                st.info("Aucun skill profil détecté — relance un scrape.")

        with col_b:
            st.markdown("**📚 Skills hors profil (gap)**")
            st.caption(
                "Souvent demandés, pas dans ton parcours. Pistes d'upskilling."
            )
            if gap_counts:
                gap_df = pd.DataFrame(
                    [
                        {
                            "Skill": skill,
                            "Offres": count,
                            "%": round(100 * count / n_offers),
                        }
                        for skill, count in gap_counts
                    ]
                )
                st.dataframe(
                    gap_df,
                    hide_index=True,
                    use_container_width=True,
                    height=min(35 * (len(gap_df) + 1) + 3, 440),
                    column_config={
                        "Offres": st.column_config.ProgressColumn(
                            "Offres",
                            format="%d",
                            min_value=0,
                            max_value=max(c for _, c in gap_counts),
                        ),
                        "%": st.column_config.NumberColumn("%", format="%d %%"),
                    },
                )
            else:
                st.info("Pas de skill gap détecté.")

        with col_c:
            st.markdown("**🎓 Certifications citées**")
            st.caption(
                "Mentions dans les descriptions d'offres. Les certifs du haut "
                "sont à envisager en priorité."
            )
            if cert_counts:
                cert_df = pd.DataFrame(
                    [
                        {
                            "Certification": cert,
                            "Offres": count,
                            "%": round(100 * count / n_offers),
                        }
                        for cert, count in cert_counts
                    ]
                )
                st.dataframe(
                    cert_df,
                    hide_index=True,
                    use_container_width=True,
                    height=min(35 * (len(cert_df) + 1) + 3, 440),
                    column_config={
                        "Offres": st.column_config.ProgressColumn(
                            "Offres",
                            format="%d",
                            min_value=0,
                            max_value=max(c for _, c in cert_counts),
                        ),
                        "%": st.column_config.NumberColumn("%", format="%d %%"),
                    },
                )
            else:
                st.info(
                    "Aucune certification explicitement citée dans les offres "
                    "actuelles."
                )

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

    st.caption(
        "Clique sur une ligne pour afficher le détail de l'offre + lien direct "
        "vers la page de candidature."
    )

    table_event = st.dataframe(
        display,
        hide_index=True,
        use_container_width=True,
        height=520,
        on_select="rerun",
        selection_mode="single-row",
        key="missions_table",
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

    # ----------------------------------------------------------------------
    # Detail panel — appears below the table when a row is selected
    # ----------------------------------------------------------------------

    selected_rows = (
        table_event.selection.rows
        if hasattr(table_event, "selection")
        else []
    )

    if selected_rows:
        idx = selected_rows[0]
        row = filtered.iloc[idx]

        st.divider()

        # Header line: title + grade badge
        header_col, badge_col = st.columns([5, 1])
        with header_col:
            st.markdown(f"### {row['title']}")
            st.caption(
                f"**{row['company']}**  ·  source : **{row['source']}**  ·  "
                f"scrapée le {row['date_scraped']}"
            )
        with badge_col:
            grade = row["match_grade"]
            pct = int(row["match_pct"])
            st.metric(label=f"Grade {grade}", value=f"{pct}%")

        # Action buttons (the main reason this panel exists)
        action_col1, action_col2, _ = st.columns([2, 2, 4])
        with action_col1:
            st.link_button(
                "📨 Voir l'offre / Postuler ↗",
                url=row["url"],
                use_container_width=True,
                type="primary",
            )
        with action_col2:
            if row.get("source_url"):
                st.link_button(
                    "🔗 Site source ↗",
                    url=row["source_url"],
                    use_container_width=True,
                )

        # Skills + score breakdown
        st.markdown("**Skills matchés depuis ton profil :**")
        skills = row.get("matched_skills") or "—"
        st.write(skills)

        all_keywords = row.get("keywords") or ""
        if all_keywords and all_keywords != skills:
            with st.expander("Tous les keywords matchés (skills + domaines + flags)"):
                st.write(all_keywords)

        # Full summary (often longer than what fits in the table cell)
        st.markdown("**Résumé de l'offre :**")
        summary_text = row.get("summary") or "_(Pas de résumé fourni par la source.)_"
        st.write(summary_text)

        # Raw URL displayed clearly (in case the user wants to copy it)
        st.caption(f"URL : `{row['url']}`")

