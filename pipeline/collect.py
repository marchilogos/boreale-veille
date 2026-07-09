#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BORÉALE — pipeline de veille « saison de travail »
Collecte → dédup → mémoire des rejets → portes (règles) → scoring IA → docs/data/offers.json

Principes (brief §14) : pré-filtrer par règles avant tout appel LLM ; batcher ;
ne jamais retraiter une offre déjà vue ; modèle fort réservé aux tops.
Sans clé API : repli sur un scoring heuristique, marqué « provisoire ».
"""
import datetime
import hashlib
import json
import os
import re
import sys

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {"User-Agent": "Mozilla/5.0 (compatible; BorealeVeille/1.0; +https://github.com/marchilogos/boreale-veille)"}
NOW = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="minutes")
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()


def p(rel):
    return os.path.join(ROOT, rel)


def jload(rel, default):
    try:
        with open(p(rel), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def jsave(rel, obj):
    with open(p(rel), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


CAL = yaml.safe_load(open(p("calibrage.yml"), encoding="utf-8"))
SRC = yaml.safe_load(open(p("sources.yml"), encoding="utf-8"))["sources"]
seen = jload("data/seen.json", {})
rejects = jload("data/rejects.json", [])          # titres/urls écartés par Jo & Elle -> jamais de récurrence
health = jload("data/health.json", {})
current = jload("docs/data/offers.json", {"meta": {}, "offers": [], "humans": [], "rejected": []})


# ------------------------------------------------------------------ collecte
def fetch(url):
    r = requests.get(url, headers=UA, timeout=25)
    r.raise_for_status()
    return r.text


def collect_links(src):
    """Collecteur générique : liens contenant link_contains, titre matchant title_any."""
    html = fetch(src["url"])
    soup = BeautifulSoup(html, "html.parser")
    base = re.match(r"https?://[^/]+", src["url"]).group(0)
    out, seen_urls = [], set()
    for a in soup.select("a[href]"):
        href = a["href"]
        if src.get("link_contains") and src["link_contains"] not in href:
            continue
        title = a.get_text(" ", strip=True)
        if len(title) < 18 or len(title) > 200:
            continue
        tl = title.lower()
        if src.get("title_any") and not any(w.lower() in tl for w in src["title_any"]):
            continue
        url = href if href.startswith("http") else base + ("" if href.startswith("/") else "/") + href
        if url in seen_urls:
            continue
        seen_urls.add(url)
        out.append({"title": title[:160], "url": url, "src": src["id"],
                    "emitter": src["nom"], "geo": src.get("geo", "eu")})
    return out


collected, alerts = [], []
for src in SRC:
    if not src.get("enabled"):
        continue
    h = health.get(src["id"], {"fails": 0})
    try:
        items = collect_links(src)
        h = {"fails": 0, "last_ok": NOW, "last_count": len(items)}
        if not items:
            h["fails"] = health.get(src["id"], {}).get("fails", 0) + 1
        collected += items
    except Exception as e:  # une source qui casse ne casse jamais le passage
        h["fails"] = h.get("fails", 0) + 1
        h["last_err"] = str(e)[:200]
    if h.get("fails", 0) >= 4:  # ~2 jours muette
        alerts.append(f"Source muette/cassée : {src['nom']} ({h.get('last_err', '0 résultat')})")
    health[src["id"]] = h

# ------------------------------------------------- dédup + mémoire des rejets
def key(o):
    return hashlib.sha1((o["title"].lower().strip() + "|" + o["url"]).encode()).hexdigest()[:16]


rejset = {r.lower() for r in rejects}
fresh = []
for o in collected:
    k = key(o)
    if k in seen:
        continue                                  # jamais retraiter une offre vue
    tl = o["title"].lower()
    if k in rejset or o["url"].lower() in rejset or any(rt and rt in tl for rt in rejset):
        seen[k] = NOW                             # écartée par vous -> mémorisée, ne revient JAMAIS
        continue
    seen[k] = NOW
    o["id"] = k
    fresh.append(o)

# ------------------------------------------------------------ portes (règles)
NON_PAYE = ["bénévol", "volontari", "workaway", "wwoof", "non rémunéré", "unpaid", "voluntary", "au pair"]
HORS_FENETRE = ["saison été", "summer season", "juin-sept", "été 2026"]


def porte(o):
    t = o["title"].lower()
    if any(w in t for w in NON_PAYE):
        return "Non payé — l'argent est roi"
    if any(w in t for w in HORS_FENETRE):
        return "Hors fenêtre oct-avril"
    return None


kept, auto_rej = [], []
for o in fresh:
    r = porte(o)
    (auto_rej.append({"title": o["title"], "src": o["emitter"], "reason": r}) if r else kept.append(o))


# ------------------------------------------------------------------- scoring
def llm(model, system, user, max_tokens=4096):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": model, "max_tokens": max_tokens, "system": system,
              "messages": [{"role": "user", "content": user}]},
        timeout=180,
    )
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"])


def json_block(txt):
    m = re.search(r"\[.*\]|\{.*\}", txt, re.S)
    return json.loads(m.group(0)) if m else None


BAREME = (
    "Tu scores des offres d'emploi saisonnier pour un couple FR (elle: cheffe de projet; lui: hospitality/cuisine "
    "maisonnée, fuit la cuisine de resto; TDAH: éviter forte pression). Barème /100 : épargne 35 (net à deux/saison: "
    "15K viable, 20K content, 50K jackpot), logement 20 (chambre privée quasi-exigée, dortoir malus lourd), "
    "planque/basse pression 15, cadre nature 10, fit couple 10, travail aimé 5, friction 5. "
    "PORTES: non payé=exclure; hors fenêtre oct-avril=exclure; CDI=exclure sauf rémunération hors du commun (le noter); "
    "durée cible 1-6 mois. GÉO: fr/eu prioritaires, far (Laponie, lointain)=secondaire, hors-Schengen=malus lourd. "
    f"Journal de calibrage: {json.dumps(CAL.get('journal', []), ensure_ascii=False, default=str)}"
)

scored, mode = [], "heuristique (clé API absente — scores provisoires)"
if kept and API_KEY:
    mode = "IA (coarse en lot + fin sur les tops)"
    batch = [{"i": i, "titre": o["title"], "source": o["emitter"], "geo": o["geo"]} for i, o in enumerate(kept)]
    try:
        out = llm(CAL["modeles"]["coarse"], BAREME,
                  "Score grossier de ces offres (titre seul). Réponds UNIQUEMENT un tableau JSON "
                  '[{"i":0,"score":0-100,"exclure":false,"raison_exclusion":null,"geo":"fr|eu|far"}] :\n'
                  + json.dumps(batch, ensure_ascii=False))
        for row in json_block(out) or []:
            o = kept[row["i"]]
            if row.get("exclure"):
                auto_rej.append({"title": o["title"], "src": o["emitter"],
                                 "reason": row.get("raison_exclusion") or "règle du calibrage"})
            else:
                o["score"] = int(row["score"])
                o["geo"] = row.get("geo", o["geo"])
                scored.append(o)
    except Exception as e:
        alerts.append(f"Scoring coarse en échec ({str(e)[:120]}) — repli heuristique")

if kept and not scored:  # repli heuristique (pas de clé, ou échec API)
    BONUS = {"couple": 14, "gardien": 12, "logé": 12, "loge": 8, "saison": 8, "château": 6, "domaine": 6,
             "chef": 5, "host": 5, "chalet": 5, "propriété": 6, "caretaker": 10}
    for o in kept:
        s = 40 + sum(v for w, v in BONUS.items() if w in o["title"].lower())
        s -= 10 if o["geo"] == "far" else 0
        o["score"] = max(20, min(88, s))
        o["provisoire"] = True
        scored.append(o)

# ------- passe fine (modèle fort) sur les meilleurs nouveaux uniquement
top_new = sorted(scored, key=lambda o: -o["score"])[: CAL["modeles"].get("top_n_fin", 6)]
if API_KEY:
    for o in top_new:
        try:
            page = re.sub(r"\s+", " ", BeautifulSoup(fetch(o["url"]), "html.parser").get_text(" "))[:4000]
        except Exception:
            page = "(page inaccessible — scorer sur le titre)"
        try:
            out = llm(CAL["modeles"]["fin"], BAREME,
                      "Analyse fine de cette offre pour la carte du cockpit. Réponds UNIQUEMENT un objet JSON "
                      '{"score":0-100,"geo":"fr|eu|far","badges":["💰 …","🏠 …","📅 …"],'
                      '"why":"pourquoi ce rang, 2-3 phrases, transparent","caveat":"réserve honnête ou null",'
                      '"bars":{"Épargne":0-100,"Logement":0-100,"Planque/pression":0-100,"Cadre":0-100,'
                      '"Fit couple":0-100,"Travail aimé":0-100,"Friction":0-100},"exclure":false,"raison_exclusion":null}'
                      f"\n\nTITRE: {o['title']}\nSOURCE: {o['emitter']}\nURL: {o['url']}\nPAGE: {page}")
            d = json_block(out)
            if d:
                if d.get("exclure"):
                    scored.remove(o)
                    auto_rej.append({"title": o["title"], "src": o["emitter"],
                                     "reason": d.get("raison_exclusion") or "règle du calibrage"})
                    continue
                o.update({k: d[k] for k in ("score", "geo", "badges", "why", "caveat", "bars") if d.get(k) is not None})
                o["verif"] = {"lvl": "ok", "txt": f"✓ vérifiée — page ouverte au passage du {NOW[:16].replace('T', ' ')}"}
        except Exception as e:
            alerts.append(f"Passe fine en échec sur « {o['title'][:50]} » ({str(e)[:80]})")

# ------------------------------------------------------------------- fusion
for o in scored:
    o.setdefault("tier", "top" if o["score"] >= 76 else "flow")
    o.setdefault("cat", ["couple"] if "couple" in o["title"].lower() else [])
    o.setdefault("canal", "Board")
    o.setdefault("verif", {"lvl": "listed", "txt": "◌ listée — trouvée au passage automatique, page non ouverte"})
    o.setdefault("badges", [])
    o.setdefault("why", "Score provisoire sur le titre seul — la passe fine (modèle fort) détaillera au prochain passage."
                 if o.get("provisoire") else "")
    o.setdefault("bars", {})
    o["nouveau"] = NOW[:10]

known_urls = {o["url"] for o in current.get("offers", [])}
merged = current.get("offers", []) + [o for o in scored if o["url"] not in known_urls]

# expiration douce : les offres non shortlistées de plus de 21 jours sortent du flux
def fresh_enough(o):
    d = o.get("nouveau") or "2026-07-09"
    try:
        age = (datetime.date.today() - datetime.date.fromisoformat(d)).days
    except Exception:
        age = 0
    return age <= 21


merged = [o for o in merged if fresh_enough(o)]
merged.sort(key=lambda o: -o.get("score", 0))

rejected_list = (current.get("rejected", []) + auto_rej)[-25:]

meta = {
    "run_at": NOW, "run_type": f"passage automatique — scoring {mode}",
    "examined": len(collected), "new": len(fresh), "kept": len(merged),
    "humans": len(current.get("humans", [])), "rejected": len(rejected_list),
    "calibrage": f"v{CAL.get('version')}", "alerts": alerts,
    "sources_sante": {k: ("ok" if v.get("fails", 0) == 0 else f"{v['fails']} échecs") for k, v in health.items()},
}

jsave("docs/data/offers.json", {"meta": meta, "offers": merged,
                                "humans": current.get("humans", []), "rejected": rejected_list})
jsave("data/seen.json", seen)
jsave("data/health.json", health)

print(f"[boréale] {NOW} · examinées {len(collected)} · nouvelles {len(fresh)} · gardées {len(merged)} · "
      f"écartées {len(auto_rej)} · scoring {mode} · alertes {len(alerts)}")
if alerts:
    print("\n".join(" ! " + a for a in alerts))
    sys.exit(0)
