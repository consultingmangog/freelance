# Freelance Job Board — "Zero-Cost"

Un job board personnel, 100 % automatisé et hébergé gratuitement, pour dénicher
des missions freelances Data Engineer / GCP / Remote.

- **Scraping** : flux RSS uniquement (aucune IP bannie, aucun blocage Cloudflare)
- **Scoring** : chaque offre reçoit un score de matching expérience (0–100 %)
  et un grade A/B/C/D pour voir d'un coup d'œil les chances d'être retenu
- **Stockage** : simple fichier `missions.csv` versionné
- **Automatisation** : GitHub Actions tourne tous les jours à 07:00 UTC
  (~ 08:00 Paris en hiver, 09:00 en été) et commit le CSV mis à jour
- **Interface** : dashboard Streamlit minimaliste, hébergé gratuitement sur
  Streamlit Community Cloud

## Architecture

```
freelance/
├── .github/workflows/scrape.yml   # Cron GitHub Actions (daily 07:00 UTC)
├── scraper.py                     # Fetch RSS + scoring + dedup
├── app.py                         # Dashboard Streamlit
├── missions.csv                   # Données (créé/mis à jour par le scraper)
├── requirements.txt               # Dépendances Python
├── .gitignore
└── README.md
```

## Sources de données (RSS vérifiés)

| Source | Zone | Flux |
|---|---|---|
| Remotive | Intl | https://remotive.com/remote-jobs/feed |
| WeWorkRemotely | Intl | https://weworkremotely.com/categories/remote-programming-jobs.rss |
| RemoteOK | Intl | https://remoteok.com/remote-dev-jobs.rss |
| Himalayas | Intl | https://himalayas.app/jobs/rss?search=data+engineer |
| Jobicy | Intl | https://jobicy.com/?feed=job_feed |
| Codeur | FR | https://www.codeur.com/projects.rss |

Pour ajouter une source, éditer simplement `RSS_SOURCES` dans `scraper.py`.

## Logique de scoring

1. **Inclusion** — l'offre doit contenir au moins un mot-clé parmi :
   `data engineer`, `analytics engineer`, `gcp`, `google cloud`, `bigquery`,
   `hive`, `hadoop`, `python`, `remote`, `dbt`, `snowflake`, `airflow`,
   `data warehouse`, `etl`.

2. **Scoring** :
   - `+3` si `data engineer` dans le titre
   - `+2` si `analytics engineer` dans le titre
   - `+1` si `data` + (`engineer`|`pipeline`|`platform`) dans le titre
   - `+2` par skill core matché (dbt, gcp, bigquery, hive, hadoop,
     snowflake, redshift, data warehouse, dwh, airflow)
   - `+2` si remote / télétravail
   - `+1` si mention de séniorité (senior, lead, confirmé, principal, staff)

3. **Rejets durs** :
   - AWS-only (sans mention GCP) → rejeté
   - Azure-only (sans mention GCP) → rejeté
   - On-site obligatoire (sans mention remote) → rejeté

4. **Normalisation** : `match_pct = min(100, score × 10)`

5. **Grade** : A (≥ 80 %), B (50–79 %), C (30–49 %), D (< 30 %)

## Démarrage local

```bash
# 1. Créer un venv (optionnel mais recommandé)
python -m venv .venv
source .venv/bin/activate

# 2. Installer les dépendances
pip install -r requirements.txt

# 3. Lancer le scraper (remplit ou met à jour missions.csv)
python scraper.py

# 4. Lancer le dashboard
streamlit run app.py
```

Le dashboard s'ouvre sur `http://localhost:8501`.

## Déploiement

### 1. Créer le dépôt GitHub

```bash
git init -b main
git add .
git commit -m "feat: scaffold job board freelance"
git remote add origin git@github.com:<TON-USER>/<TON-REPO>.git
git push -u origin main
```

### 2. Activer GitHub Actions

Le workflow `.github/workflows/scrape.yml` est déclenché automatiquement :

- **Cron** : tous les jours à 07:00 UTC
- **Manuel** : onglet **Actions → Daily Scrape → Run workflow**

Le workflow a besoin de pouvoir committer le CSV mis à jour :

1. Dans le repo, va dans **Settings → Actions → General**
2. Section **Workflow permissions** → coche **Read and write permissions**
3. Sauvegarde

### 3. Déployer l'interface sur Streamlit Community Cloud

1. Va sur https://share.streamlit.io et connecte-toi avec GitHub.
2. Clique sur **New app**.
3. Sélectionne ton dépôt et la branche `main`.
4. Main file path : `app.py`
5. Clique sur **Deploy**.

L'URL publique de type `https://<ton-app>.streamlit.app` est générée en
quelques secondes. Chaque commit du scraper (via GitHub Actions) rafraîchit
automatiquement le CSV ; le cache Streamlit expire toutes les 5 minutes.

### 4. (Optionnel) Trigger manuel du scraper depuis Streamlit

Le dashboard expose deux boutons dans la sidebar :

- **⟳ Rafraîchir** — vide le cache et relit `missions.csv` immédiatement.
- **▶ Scraper** — déclenche le workflow `Daily Scrape` via l'API GitHub
  (`workflow_dispatch`). Nécessite un token côté Streamlit Cloud.

Pour activer le bouton **▶ Scraper** :

1. Crée un Personal Access Token GitHub (fine-grained) avec la permission
   **Actions: Read and write** sur le dépôt. https://github.com/settings/personal-access-tokens/new
2. Dans Streamlit Community Cloud → ton app → **Settings → Secrets**, ajoute :

   ```toml
   GITHUB_TOKEN = "ghp_xxxxxxxxxxxx"
   GITHUB_REPO  = "consultingmangog/freelance"
   GITHUB_BRANCH = "claude/job-board-automation-yTvBX"   # optionnel
   ```

3. Sauvegarde. Le bouton fonctionne instantanément, sans redéployer.

Sans ces secrets, le bouton affiche simplement un message t'invitant à les
configurer — l'app continue de fonctionner normalement.

Tu peux aussi toujours déclencher le workflow manuellement depuis l'onglet
**Actions → Daily Scrape → Run workflow** sur GitHub, ce qui ne nécessite
aucun secret.

## Dépendances

Voir `requirements.txt` :

```
streamlit>=1.32.0
pandas>=2.2.0
requests>=2.31.0
beautifulsoup4>=4.12.0
feedparser>=6.0.11
lxml>=5.1.0
```

## Sécurité

- Aucune donnée personnelle dans le code (ni email, ni téléphone, ni nom).
- Seuls les mots-clés techniques génériques sont versionnés.
- Le User-Agent utilisé est générique (`JobBoardBot/1.0`).
- Le CSV committé ne contient que des liens publics d'offres — aucun secret.
- Les règles de scoring et d'exclusion sont intégralement dans `scraper.py` et
  peuvent être ajustées sans toucher au reste.

## Personnalisation rapide

| Paramètre | Fichier | Variable |
|---|---|---|
| Sources RSS | `scraper.py` | `RSS_SOURCES` |
| Mots-clés d'inclusion | `scraper.py` | `INCLUSION_KEYWORDS` |
| Skills core (scoring) | `scraper.py` | `CORE_SKILLS` |
| Termes d'exclusion on-site | `scraper.py` | `ONSITE_TERMS` |
| Seuil minimum de rétention | `scraper.py` | `MIN_SCORE_TO_KEEP` |
| Heure du cron | `.github/workflows/scrape.yml` | `cron: "0 7 * * *"` |

## Licence

Projet personnel — à adapter à ta convenance.
