# Tableau de bord — Global Growth Cycle & Growth Trend Timing

Deux fichiers : `collect.py` fabrique les données, `dashboard.html` les affiche.
La page fonctionne dès maintenant en double-cliquant dessus (elle affiche alors
un jeu de démonstration, signalé par un bandeau).

## Mise en route

```bash
# clé FRED gratuite : https://fredaccount.stlouisfed.org/apikeys
export FRED_API_KEY=votre_cle

python3 collect.py --verbose     # écrit data.json à côté du script
python3 -m http.server 8000      # puis ouvrir http://localhost:8000/dashboard.html
```

Le serveur local est nécessaire : ouvert en `file://`, le navigateur refuse de
lire `data.json` et la page bascule sur les données de démonstration.

## Sources

| Donnée | Source | Identifiant | Fréquence de publication |
|---|---|---|---|
| CLI par pays (17 économies) | API SDMX OCDE, sans clé | `DSD_STES@DF_CLI`, mesure `LI`, unité `AA` | mensuelle, ~7 du mois |
| repli CLI | FRED | `USALOLITOAASTSAM`, `FRALOLITOAASTSAM`… | idem |
| Production industrielle | FRED | `INDPRO` | vers le 15 du mois |
| Ventes de détail réelles | FRED | `RRSFS` (= `RSAFS` déflaté par `CPIAUCSL`) | vers le 15 du mois |
| S&P 500 quotidien | Stooq (CSV), repli FRED `SP500` | `^spx` | clôture J-1 |

Calendrier OCDE 2026 : 7 juillet, **pas de publication en août** (juillet et août
paraissent le 7 septembre), puis 7 octobre, 9 novembre, 7 décembre.

## Mise à jour automatique

Dépôt GitHub + GitHub Pages, gratuit. Ajouter `.github/workflows/update.yml` :

```yaml
name: update
on:
  schedule:
    - cron: "30 6 * * 1-5"   # tous les jours ouvrés, 6 h 30 UTC
  workflow_dispatch:
jobs:
  build:
    runs-on: ubuntu-latest
    permissions: { contents: write }
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: python collect.py
        env:
          FRED_API_KEY: ${{ secrets.FRED_API_KEY }}
      - run: |
          git config user.name  github-actions
          git config user.email actions@github.com
          git add data.json
          git diff --quiet --cached || git commit -m "données du $(date -I)"
          git push
```

Activer Pages sur la branche `main` : la page est alors consultable depuis
n'importe quel appareil, toujours à jour.

## Points de méthode à garder en tête

**L'indice de diffusion est calculé, pas publié.** L'OCDE ne diffuse que les CLI
par pays ; l'indice de diffusion est la part des pays dont le CLI progresse d'un
mois sur l'autre. Le périmètre est passé de 39 à 17 économies en 2022-2023 : les
séries d'avant et d'après ne sont pas directement comparables, ce qui explique
les difficultés rencontrées lors d'un backtest. Le nombre impair (17) garantit
qu'on ne tombe jamais exactement sur 50 %.

**Révisions.** CLI, INDPRO et RRSFS sont révisés pendant plusieurs mois. Un
signal passé peut donc changer rétrospectivement — c'est le biais de
« lookahead » que Link signale lui-même. Pour un backtest honnête, il faut les
millésimes d'origine : FRED expose ALFRED via les paramètres `realtime_start`
et `realtime_end` du même point d'API, et l'OCDE ne conserve pas d'archive
comparable (d'où l'intérêt d'archiver `data.json` à chaque exécution — ce que
fait le workflow ci-dessus, par l'historique git).

**Trou dans RRSFS.** La série présente une valeur manquante en octobre 2025.
Le calcul de glissement annuel renvoie alors `None` et la page affiche « n.d. »
plutôt qu'un faux signal.

**Dates d'évaluation.** Elles ne coïncident pas : GGC s'évalue le 15 du mois
(Link décale les données de 15 jours pour couvrir le délai de publication de
l'OCDE, de 9 à 14 jours), GTT à la clôture du dernier jour de bourse du mois.
