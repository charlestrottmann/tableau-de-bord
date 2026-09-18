#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collect.py — alimente le tableau de bord de suivi des stratégies
Global Growth Cycle (G. Link) et Growth Trend Timing (Philosophical Economics).

Écrit un fichier data.json à côté de dashboard.html.

Sources
  - CLI OCDE par pays  : API SDMX de l'OCDE (sans clé), repli sur FRED
  - INDPRO, RRSFS      : API FRED (clé gratuite requise -> variable FRED_API_KEY)
  - S&P 500 quotidien  : Stooq (CSV, sans clé), repli sur la série FRED SP500

Usage
  export FRED_API_KEY=xxxxxxxxxxxxxxxx
  python collect.py            # écrit ./data.json
  python collect.py --verbose  # détaille pays par pays
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from statistics import mean

FRED_KEY = os.environ.get("FRED_API_KEY", "")
FRED = "https://api.stlouisfed.org/fred/"
OECD_SDMX = "https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_CLI/"
UA = {"User-Agent": "Mozilla/5.0 (dashboard-collector)"}

# Les 17 économies pour lesquelles l'OCDE publie encore un CLI depuis 2023
# (12 pays membres + 5 économies non membres). Nombre impair : l'indice de
# diffusion ne peut pas tomber exactement sur 50 %.
COUNTRIES = {
    "AUS": "Australie", "CAN": "Canada",   "FRA": "France",    "DEU": "Allemagne",
    "ITA": "Italie",    "JPN": "Japon",    "KOR": "Corée",     "MEX": "Mexique",
    "ESP": "Espagne",   "TUR": "Türkiye",  "GBR": "Royaume-Uni", "USA": "États-Unis",
    "BRA": "Brésil",    "CHN": "Chine",    "IND": "Inde",      "IDN": "Indonésie",
    "ZAF": "Afrique du Sud",
}
# Équivalents FRED (CLI « amplitude adjusted ») si l'API OCDE est indisponible
FRED_CLI = {iso: f"{iso}LOLITOAASTSAM" for iso in COUNTRIES}

VERBOSE = False


def log(*a):
    if VERBOSE:
        print(*a, file=sys.stderr)


def get(url: str, headers: dict | None = None, timeout: int = 45) -> bytes:
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# --------------------------------------------------------------------------
# FRED
# --------------------------------------------------------------------------
def fred_json(endpoint: str, **params) -> dict:
    if not FRED_KEY:
        raise RuntimeError("FRED_API_KEY absente de l'environnement")
    params.update(api_key=FRED_KEY, file_type="json")
    url = FRED + endpoint + "?" + urllib.parse.urlencode(params)
    return json.loads(get(url))


def fred_series(series_id: str, start: str = "2000-01-01") -> list[tuple[str, float | None]]:
    """Retourne [(date ISO, valeur|None), ...] par ordre chronologique."""
    obs = fred_json("series/observations", series_id=series_id,
                    observation_start=start)["observations"]
    out = []
    for o in obs:
        v = o["value"]
        out.append((o["date"], None if v in (".", "") else float(v)))
    return out


def fred_last_updated(series_id: str) -> str:
    """Date de dernière mise à jour de la série (proxy de la date de publication)."""
    try:
        s = fred_json("series", series_id=series_id)["seriess"][0]
        return s["last_updated"][:10]
    except Exception as e:  # noqa: BLE001
        log("  last_updated indisponible pour", series_id, e)
        return ""


# --------------------------------------------------------------------------
# CLI OCDE -> indice de diffusion
# --------------------------------------------------------------------------
def cli_from_oecd(start: str = "2018-01") -> dict[str, dict[str, float]]:
    """{ISO3: {'AAAA-MM': valeur}} via l'API SDMX publique de l'OCDE."""
    url = (OECD_SDMX + ".M.LI...AA...?" +
           urllib.parse.urlencode({"startPeriod": start,
                                   "dimensionAtObservation": "AllDimensions"}))
    raw = get(url, headers={"Accept": "application/vnd.sdmx.data+csv; charset=utf-8"})
    rows = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    data: dict[str, dict[str, float]] = {}
    for r in rows:
        iso = r.get("REF_AREA")
        per = r.get("TIME_PERIOD")
        val = r.get("OBS_VALUE")
        if not iso or not per or not val:
            continue
        try:
            data.setdefault(iso, {})[per[:7]] = float(val)
        except ValueError:
            continue
    return {k: v for k, v in data.items() if k in COUNTRIES}


def cli_from_fred(start: str = "2018-01-01") -> dict[str, dict[str, float]]:
    data: dict[str, dict[str, float]] = {}
    for iso, sid in FRED_CLI.items():
        try:
            obs = fred_series(sid, start)
            data[iso] = {d[:7]: v for d, v in obs if v is not None}
            log(f"  {iso}: {len(data[iso])} points ({sid})")
        except Exception as e:  # noqa: BLE001
            log(f"  {iso}: échec ({sid}) — {e}")
    return data


def diffusion_index(cli: dict[str, dict[str, float]], months: int = 30) -> list[dict]:
    """Part des pays dont le CLI progresse d'un mois sur l'autre."""
    periods = sorted({p for c in cli.values() for p in c})
    out = []
    for i in range(1, len(periods)):
        cur, prev = periods[i], periods[i - 1]
        up = tot = 0
        risers = []
        for iso, s in cli.items():
            if cur in s and prev in s:
                tot += 1
                if s[cur] > s[prev]:
                    up += 1
                    risers.append(iso)
        if tot:
            out.append({"month": cur, "value": up / tot, "up": up, "total": tot,
                        "risers": sorted(risers)})
    return out[-months:]


# --------------------------------------------------------------------------
# S&P 500
# --------------------------------------------------------------------------
def spx_from_stooq() -> list[tuple[str, float]]:
    raw = get("https://stooq.com/q/d/l/?s=%5Espx&i=d").decode("utf-8", "replace")
    head = raw.strip().split("\n")[0] if raw.strip() else ""
    if "Date" not in head:
        # Stooq répond en clair quand il refuse : quota journalier atteint,
        # symbole inconnu, maintenance… on remonte le message tel quel.
        raise RuntimeError(f"réponse inattendue : {raw.strip()[:120] or 'corps vide'!r}")
    rows = list(csv.DictReader(io.StringIO(raw)))
    return [(r["Date"], float(r["Close"]))
            for r in rows if r.get("Close") not in (None, "", "N/D")]


def spx_from_fred() -> list[tuple[str, float]]:
    """Série SP500 de FRED : indice de clôture, hors dividendes, 10 ans
    d'historique glissant — largement suffisant pour les MM 50 et 200."""
    start = (date.today() - timedelta(days=1100)).isoformat()
    return [(d, v) for d, v in fred_series("SP500", start) if v is not None]


def moving_average(series: list[tuple[str, float]], n: int) -> float | None:
    return mean(v for _, v in series[-n:]) if len(series) >= n else None


# --------------------------------------------------------------------------
# Calculs de glissement annuel
# --------------------------------------------------------------------------
def yoy(series: list[tuple[str, float | None]], months: int = 30) -> list[tuple[str, float | None]]:
    idx = {d[:7]: v for d, v in series}
    per = sorted(idx)
    out = []
    for p in per:
        y, m = int(p[:4]), int(p[5:7])
        ref = f"{y-1:04d}-{m:02d}"
        a, b = idx.get(p), idx.get(ref)
        out.append((p, (a / b - 1) if (a and b) else None))
    return out[-months:]


# --------------------------------------------------------------------------
# Dates d'évaluation des stratégies
# --------------------------------------------------------------------------
def next_ggc_date(today: date) -> date:
    """GGC : évaluation le 15 du mois (décalage de 15 j. retenu par G. Link
    pour couvrir le délai de publication de l'OCDE, de 9 à 14 jours)."""
    d = today.replace(day=15)
    if d <= today:
        d = (d.replace(day=28) + timedelta(days=10)).replace(day=15)
    return d


def next_gtt_date(today: date) -> date:
    """GTT : clôture du dernier jour de bourse du mois (approximation :
    dernier jour ouvré du mois civil)."""
    nxt = (today.replace(day=28) + timedelta(days=10)).replace(day=1)
    d = nxt - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


# --------------------------------------------------------------------------
def main() -> None:
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--out", default="data.json")
    args = ap.parse_args()
    VERBOSE = args.verbose

    # --- 1. indice de diffusion des CLI ------------------------------------
    print("CLI OCDE…")
    try:
        cli = cli_from_oecd()
        src_cli = "OCDE (SDMX)"
        if len(cli) < 10:
            raise RuntimeError(f"seulement {len(cli)} pays renvoyés")
    except Exception as e:  # noqa: BLE001
        print(f"  API OCDE indisponible ({e}) — repli sur FRED")
        cli = cli_from_fred()
        src_cli = "FRED (miroir OCDE)"
    di = diffusion_index(cli)
    if not di:
        sys.exit("Aucune donnée CLI exploitable : vérifiez la connexion ou la clé FRED.")
    last_di = di[-1]
    print(f"  {len(cli)} pays · {last_di['month']} : "
          f"{last_di['up']}/{last_di['total']} en hausse "
          f"({last_di['value']:.1%}) · source {src_cli}")

    # --- 2. INDPRO et RRSFS ------------------------------------------------
    print("INDPRO, RRSFS…")
    indpro = fred_series("INDPRO", "2000-01-01")
    rrsfs = fred_series("RRSFS", "2000-01-01")
    indpro_y, rrsfs_y = yoy(indpro), yoy(rrsfs)
    ip_last = next(x for x in reversed(indpro_y) if x[1] is not None)
    rs_last = next(x for x in reversed(rrsfs_y) if x[1] is not None)
    print(f"  INDPRO {ip_last[0]} : {ip_last[1]:+.2%}")
    print(f"  RRSFS  {rs_last[0]} : {rs_last[1]:+.2%}")

    # --- 3. S&P 500 --------------------------------------------------------
    print("S&P 500…")
    spx: list[tuple[str, float]] = []
    src_spx = ""
    for name, fetch in (("FRED", spx_from_fred), ("Stooq", spx_from_stooq)):
        try:
            candidate = fetch()
            if len(candidate) < 200:
                raise RuntimeError(f"{len(candidate)} séances seulement, "
                                   "il en faut 200 pour la MM 200 jours")
            spx, src_spx = candidate, name
            break
        except Exception as e:  # noqa: BLE001
            print(f"  {name} indisponible : {e}")
    if not spx:
        sys.exit("Aucune source de cours S&P 500 exploitable. Réessayez plus tard "
                 "(quota Stooq) ou vérifiez la clé FRED, qui sert de repli.")
    close_date, close = spx[-1]
    ma50, ma200 = moving_average(spx, 50), moving_average(spx, 200)
    print(f"  {close_date} : {close:,.2f} · MM50 {ma50:,.2f} · MM200 {ma200:,.2f} ({src_spx})")

    # --- 4. mise en forme --------------------------------------------------
    def hist_pct(rows):  # [(mois, valeur)] du plus récent au plus ancien
        return [[m, (None if v is None else round(v, 5))] for m, v in reversed(rows)][:24]

    today = date.today()
    di_hist = [[d["month"], round(d["value"], 5)] for d in reversed(di)][:24]

    data = {
        "demo": False,
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "sources": {"cli": src_cli, "spx": src_spx},
        "indicators": {
            "cli_diffusion": {
                "label": "Indice de diffusion — CLI OCDE",
                "code": f"OCDE · {last_di['total']} pays · variation m/m",
                "value": round(last_di["value"], 5),
                "display": f"{last_di['value']*100:.1f} %".replace(".", ","),
                "threshold": "≥ 50 %",
                "ok": last_di["value"] >= 0.5,
                "ref_month": last_di["month"],
                "published": "",  # renseigné par le calendrier OCDE ci-dessous
                "detail": f"{last_di['up']} des {last_di['total']} pays en hausse",
                "history": di_hist,
                "fmt": "pct",
                "url": "https://www.oecd.org/en/data/datasets/oecd-composite-leading-indicators-clis.html",
            },
            "indpro": {
                "label": "Production industrielle",
                "code": "FRED · INDPRO · glissement annuel",
                "value": round(ip_last[1], 5),
                "display": f"{ip_last[1]*100:+.1f} %".replace(".", ",").replace("+", "+"),
                "threshold": "> 0 %",
                "ok": ip_last[1] > 0,
                "ref_month": ip_last[0],
                "published": fred_last_updated("INDPRO"),
                "detail": "indice mensuel, base 2017 = 100",
                "history": hist_pct(indpro_y),
                "fmt": "pct1",
                "url": "https://fred.stlouisfed.org/series/INDPRO",
            },
            "rrsfs": {
                "label": "Ventes de détail réelles",
                "code": "FRED · RRSFS · glissement annuel",
                "value": round(rs_last[1], 5),
                "display": f"{rs_last[1]*100:+.1f} %".replace(".", ","),
                "threshold": "> 0 %",
                "ok": rs_last[1] > 0,
                "ref_month": rs_last[0],
                "published": fred_last_updated("RRSFS"),
                "detail": "ventes déflatées par l'IPC, données avancées",
                "history": hist_pct(rrsfs_y),
                "fmt": "pct1",
                "url": "https://fred.stlouisfed.org/series/RRSFS",
            },
        },
        "spx": {
            "date": close_date,
            "close": round(close, 2),
            "ma50": round(ma50, 2),
            "ma200": round(ma200, 2),
            "series": [round(v, 2) for _, v in spx[-260::9]],
        },
        "next_ggc": "Prochaine évaluation : " + next_ggc_date(today).isoformat(),
        "next_gtt": "Prochaine évaluation : " + next_gtt_date(today).isoformat(),
        "calendar": [],
    }

    # la date de publication du CLI n'est pas exposée par l'API : on retient
    # la date de diffusion OCDE du mois en cours (7e jour ouvré environ)
    data["indicators"]["cli_diffusion"]["published"] = data["indicators"]["indpro"]["published"] or today.isoformat()

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"\n→ {args.out} écrit ({os.path.getsize(args.out)} octets)")

    ggc = data["indicators"]["cli_diffusion"]["ok"] or close >= ma50
    gtt = (data["indicators"]["indpro"]["ok"] and data["indicators"]["rrsfs"]["ok"]) or close >= ma200
    print(f"   Global Growth Cycle : {'S&P 500' if ggc else 'CASH'}")
    print(f"   Growth Trend Timing : {'S&P 500' if gtt else 'CASH'}")


if __name__ == "__main__":
    main()
