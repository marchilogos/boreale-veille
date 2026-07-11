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
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36",
      "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7",
      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
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


alerts = []

# ---- Boucle de feedback (palier 2) : Supabase + Notion ----
SUPA_URL = "https://bljvehfncafnqrzetjyb.supabase.co"
SUPA_ANON = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImJsanZlaGZuY2FmbnFyemV0anliIiwi"
             "cm9sZSI6ImFub24iLCJpYXQiOjE3ODM2MDk3NjIsImV4cCI6MjA5OTE4NTc2Mn0.oS_9Kh9t1GMqPYgKrh1TWv-JuVNt4SpyUpX4aWw0lVc")
SUPA_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip() or SUPA_ANON
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
NOTION_SHORTLIST_PAGE = "398c7660-640d-81b5-8b84-f6c7561c41f5"


def fetch_feedback():
    try:
        r = requests.get(SUPA_URL + "/rest/v1/feedback?select=*&order=id.asc",
                         headers={"apikey": SUPA_KEY, "Authorization": "Bearer " + SUPA_KEY}, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        alerts.append(f"Supabase illisible ({str(e)[:80]})")
        return []


def notion_shortlist_page(o):
    if not NOTION_TOKEN:
        return False
    blocks = [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": t[:1900]}}]}}
              for t in [f"Score {o.get('score', '?')} · {o.get('emitter', '')}",
                        "Lien : " + o.get("url", ""),
                        "Pourquoi : " + (o.get("why", "") or "—"),
                        "Repères : " + " · ".join(o.get("badges", [])[:6]),
                        "Réserve : " + (o.get("caveat", "") or "—")] if t.strip(" ·:—")]
    try:
        r = requests.post("https://api.notion.com/v1/pages",
                          headers={"Authorization": "Bearer " + NOTION_TOKEN,
                                   "Notion-Version": "2022-06-28", "Content-Type": "application/json"},
                          json={"parent": {"page_id": NOTION_SHORTLIST_PAGE},
                                "properties": {"title": {"title": [{"text": {"content": ("⭐ " + o["title"])[:180]}}]}},
                                "children": blocks}, timeout=30)
        if not r.ok:
            alerts.append(f"Notion refuse la page ({r.status_code}) — intégration connectée à la page Boréale ?")
        return r.ok
    except Exception as e:
        alerts.append(f"Notion inaccessible ({str(e)[:80]})")
        return False


FEEDBACK = fetch_feedback()
fb_state = None  # chargé après jload plus bas

CAL = yaml.safe_load(open(p("calibrage.yml"), encoding="utf-8"))
SRC = yaml.safe_load(open(p("sources.yml"), encoding="utf-8"))["sources"]
seen = jload("data/seen.json", {})
rejects = jload("data/rejects.json", [])          # titres/urls écartés par Jo & Elle -> jamais de récurrence
health = jload("data/health.json", {})
current = jload("docs/data/offers.json", {"meta": {}, "offers": [], "humans": [], "rejected": []})
fb_state = jload("data/feedback_state.json", {"last_id": 0, "shortlisted": []})

# Dépouillement du feedback
CONSIGNES = [f.get("note", "") for f in FEEDBACK if f.get("type") == "consigne"][-10:]
FB_REJECT_IDS = {str(f.get("offer_id")) for f in FEEDBACK if f.get("type") == "non_jamais"}
FB_REJECT_TITLES = [f.get("offer_title", "") for f in FEEDBACK if f.get("type") == "non_jamais" and f.get("offer_title")]
for t in FB_REJECT_TITLES:
    if t.lower() not in {r.lower() for r in rejects}:
        rejects.append(t)
NEW_SHORTLIST = [f for f in FEEDBACK
                 if f.get("type") == "shortlist" and f.get("id", 0) > fb_state["last_id"]
                 and str(f.get("offer_id")) not in {str(x) for x in fb_state["shortlisted"]}]


# ------------------------------------------------------------------ collecte
def fetch(url):
    r = requests.get(url, headers=UA, timeout=25)
    r.raise_for_status()
    return r.text


JUNK = ["blog", "being a ", "guide", "conseil", "article", "actualité", "témoignage", "how to", "top 10",
        "faq", "à propos", "about us", "contact", "cookie", "politique", "newsletter"]


def clean_title(t):
    t = re.sub(r"\s*(Read more|Lire la suite|Voir l'offre.*|En savoir plus)\s*$", "", t, flags=re.I)
    t = re.sub(r"\s*\d{1,2}/\d{1,2}/\d{4}.*$", "", t)          # dates de listing collées
    t = re.sub(r"\s{2,}", " ", t).strip(" -·|")
    return t.strip()


def norm(t):
    return re.sub(r"[^a-z0-9]+", "", t.lower())[:60]


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
        title = clean_title(title)
        if len(title) < 18 or any(j in title.lower() for j in JUNK):
            continue
        seen_urls.add(url)
        out.append({"title": title[:160], "url": url, "src": src["id"],
                    "emitter": src["nom"], "geo": src.get("geo", "eu")})
    return out


def collect_mail(src):
    """Lit la boîte d'alertes (Indeed, Jooble, leboncoin, EURES…) — la voie royale
    vers les sources qui bloquent les robots mais adorent envoyer des emails."""
    user = os.environ.get("MAIL_USER", "").strip()
    pwd = os.environ.get("MAIL_APP_PASSWORD", "").strip()
    if not user or not pwd:
        raise RuntimeError("secrets MAIL_USER / MAIL_APP_PASSWORD absents")
    import imaplib, email as em
    DOMS = ["indeed.", "jooble.", "leboncoin.", "europa.eu", "eures", "lhotellerie",
            "jobs.ch", "jobup.ch", "seasonworkers", "ski-jobs", "caterer", "hosco"]
    out = []
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(user, pwd)
    M.select("INBOX")
    _, data = M.search(None, "UNSEEN")
    for i in data[0].split()[-120:]:
        try:
            _, msg = M.fetch(i, "(RFC822)")
            m = em.message_from_bytes(msg[0][1])
            html = ""
            for part in m.walk():
                if part.get_content_type() == "text/html":
                    html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
                    break
            sender = (m.get("From") or "?").split("@")[-1].strip(">").strip()
            for a in BeautifulSoup(html, "html.parser").select("a[href]"):
                t = a.get_text(" ", strip=True)
                u = a["href"]
                if not (18 <= len(t) <= 160) or t.lower().startswith(("voir", "see all", "unsubscribe", "gérer")):
                    continue
                if not any(d in u.lower() for d in DOMS):
                    continue
                out.append({"title": t[:160], "url": u.split("&utm")[0][:600], "src": "mailbox",
                            "emitter": f"Alerte email · {sender}", "geo": "eu"})
            M.store(i, "+FLAGS", "\\Seen")
        except Exception:
            continue
    M.logout()
    return out


collected = []
for src in SRC:
    if not src.get("enabled"):
        continue
    h = health.get(src["id"], {"fails": 0})
    try:
        items = collect_mail(src) if src.get("collector") == "mail" else collect_links(src)
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
NON_PAYE = ["bénévol", "volontari", "workaway", "wwoof", "non rémunéré", "unpaid", "voluntary", "au pair",
            "house sit", "house-sit", "échange", "volunteer"]
CDI = ["cdi", "permanent", "year-round", "year round", "à l'année", "unbefristet", "indeterminato"]
HORS_FENETRE = ["saison été", "summer season", "juin-sept", "été 2026", "summer 2026"]


def invisible(txt):
    """CDI et non payé : suppression totale, n'apparaissent nulle part (règle v4)."""
    t = (txt or "").lower()
    return any(w in t for w in NON_PAYE) or any(w in t for w in CDI)


kept, auto_rej, silencieux = [], [], 0
for o in fresh:
    t = o["title"].lower()
    if invisible(t):
        silencieux += 1
        continue
    if any(w in t for w in HORS_FENETRE):
        auto_rej.append({"title": o["title"], "src": o["emitter"], "reason": "Hors fenêtre oct-avril"})
        continue
    kept.append(o)


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
    "maisonnée, fuit la cuisine de resto; TDAH: éviter forte pression). MÉTIERS ÉLARGIS bienvenus: veilleur de nuit/"
    "night audit (planque idéale), intendance/conciergerie de chalets, événementiel d'hiver (marchés de Noël, "
    "festivals, séminaires), resort couple (Club Med & co), gîte/chambres d'hôtes manager, domaine viticole hors "
    "vendanges, résidence d'artistes/phare/île/refuge d'hiver gardé, assistant musher. Barème /100 : épargne 35 (net à deux/saison: "
    "15K viable, 20K content, 50K jackpot), logement 20 (chambre privée quasi-exigée, dortoir malus lourd), "
    "planque/basse pression 15, cadre nature 10, fit couple 10, travail aimé 5, friction 5. "
    "PORTES ABSOLUES: CDI/permanent/à l'année => exclure=true raison 'CDI'. Non payé/échange/bénévolat => exclure=true "
    "raison 'non payé'. AUCUNE exception, jamais. Hors fenêtre oct-avril => exclure. Durée cible 1-6 mois. GÉO: fr/eu prioritaires, far (Laponie, lointain)=secondaire, hors-Schengen=malus lourd. "
    f"Journal de calibrage: {json.dumps(CAL.get('journal', []), ensure_ascii=False, default=str)} "
    f"CONSIGNES RÉCENTES DE JO & ELLE (à respecter en priorité): {json.dumps(CONSIGNES, ensure_ascii=False)}"
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
                if invisible(row.get("raison_exclusion")):
                    silencieux += 1
                else:
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
             "chef": 5, "host": 5, "chalet": 5, "propriété": 6, "caretaker": 10, "veilleur": 10,
             "night": 8, "intendan": 8, "conciergerie": 8, "manager": 5, "animateur": 6,
             "événement": 6, "event": 5, "musher": 7, "phare": 8, "refuge": 5}
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
                    if invisible(d.get("raison_exclusion")):
                        silencieux += 1
                    else:
                        auto_rej.append({"title": o["title"], "src": o["emitter"],
                                         "reason": d.get("raison_exclusion") or "règle du calibrage"})
                    continue
                o.update({k: d[k] for k in ("score", "geo", "badges", "why", "caveat", "bars") if d.get(k) is not None})
                o["verif"] = {"lvl": "ok", "txt": f"✓ vérifiée — page ouverte au passage du {NOW[:16].replace('T', ' ')}"}
        except Exception as e:
            alerts.append(f"Passe fine en échec sur « {o['title'][:50]} » ({str(e)[:80]})")

# ------------------------------------------------------------------- fusion
CATS = {"planque": ["gardien", "caretaker", "veilleur", "night", "présence"],
        "maison": ["domaine", "château", "propriété", "estate", "intendan"],
        "montagne": ["chalet", "ski", "station", "alpes"],
        "nordique": ["lapland", "arctic", "laponie", "musher"],
        "evenementiel": ["événement", "event", "festival", "séminaire", "animation", "coordinat", "régie"],
        "nuit": ["night", "veilleur", "nuit"],
        "insolite": ["phare", "île", "monastère", "refuge", "observatoire", "résidence d'artistes"],
        "couple": ["couple", "pair"]}


def categorize(title):
    t = title.lower()
    return [c for c, kws in CATS.items() if any(k in t for k in kws)]


for o in scored:
    o.setdefault("tier", "top" if o["score"] >= 76 else "flow")
    o.setdefault("cat", categorize(o["title"]))
    o.setdefault("canal", "Board")
    o.setdefault("verif", {"lvl": "listed", "txt": "◌ listée — trouvée au passage automatique, page non ouverte"})
    o.setdefault("badges", [])
    o.setdefault("why", "Score provisoire sur le titre seul — la passe fine (modèle fort) détaillera au prochain passage."
                 if o.get("provisoire") else "")
    o.setdefault("bars", {})
    o["nouveau"] = NOW[:10]

known_urls = {o["url"] for o in current.get("offers", [])}
known_titles = {norm(o["title"]) for o in current.get("offers", [])}
merged = current.get("offers", []) + [o for o in scored
                                      if o["url"] not in known_urls and norm(o["title"]) not in known_titles]

# expiration douce : les offres non shortlistées de plus de 21 jours sortent du flux
def fresh_enough(o):
    d = o.get("nouveau") or "2026-07-09"
    try:
        age = (datetime.date.today() - datetime.date.fromisoformat(d)).days
    except Exception:
        age = 0
    return age <= 21


merged = [o for o in merged if not any(j in o["title"].lower() for j in JUNK)]
merged = [o for o in merged if fresh_enough(o) and not invisible(o.get("title", "") + " " + " ".join(o.get("badges", [])) + " " + (o.get("caveat") or ""))]
# « non jamais » : retrait définitif, aucune récurrence
merged = [o for o in merged if str(o.get("id")) not in FB_REJECT_IDS
          and o["title"].lower() not in {t.lower() for t in FB_REJECT_TITLES}]

# votes & commentaires visibles par les deux
VOTES, COMMENTS = {}, {}
for f in FEEDBACK:
    oid = str(f.get("offer_id"))
    if f.get("type") == "vote":
        VOTES.setdefault(oid, {})[f.get("who", "?")] = f.get("value", "")
    if f.get("type") == "commentaire":
        COMMENTS.setdefault(oid, []).append({"who": f.get("who", "?"), "note": f.get("note", "")})
for o in merged:
    oid = str(o.get("id"))
    if oid in VOTES:
        o["votes_sync"] = VOTES[oid]
    if oid in COMMENTS:
        o["comments_sync"] = COMMENTS[oid][-6:]

# shortlist -> page Notion
for f in NEW_SHORTLIST:
    oid = str(f.get("offer_id"))
    o = next((x for x in merged if str(x.get("id")) == oid), None) or \
        {"title": f.get("offer_title", "Offre"), "url": f.get("note", ""), "emitter": "", "badges": []}
    if notion_shortlist_page(o):
        fb_state["shortlisted"].append(oid)
        if isinstance(o, dict):
            o["shortlisted"] = True
if FEEDBACK:
    fb_state["last_id"] = max(f.get("id", 0) for f in FEEDBACK)

merged.sort(key=lambda o: -o.get("score", 0))

# plafond par catégorie : une vague d'annonces ne noie pas le reste
cap = (CAL.get("volume") or {}).get("max_par_categorie", 8)
counts, capped = {}, []
for o in merged:
    c = (o.get("cat") or ["autre"])[0]
    counts[c] = counts.get(c, 0) + 1
    if counts[c] <= cap:
        capped.append(o)
merged = capped

kept_norms = {norm(o["title"]) for o in merged}
rejected_list = [r for r in (current.get("rejected", []) + auto_rej)
                 if not invisible(r.get("title", "") + " " + r.get("reason", ""))
                 and not any(j in r.get("title", "").lower() for j in JUNK)
                 and norm(r.get("title", "")) not in kept_norms][-25:]

meta = {
    "run_at": NOW, "run_type": f"passage automatique — scoring {mode}",
    "examined": len(collected), "new": len(fresh), "kept": len(merged),
    "humans": len(current.get("humans", [])), "rejected": len(rejected_list),
    "calibrage": f"v{CAL.get('version')}", "alerts": alerts,
    "consignes": CONSIGNES, "feedback_total": len(FEEDBACK), "exclus_invisibles": silencieux,
    "sources_sante": {k: ("ok" if v.get("fails", 0) == 0 else f"{v['fails']} échecs") for k, v in health.items()},
}

jsave("docs/data/offers.json", {"meta": meta, "offers": merged,
                                "humans": current.get("humans", []), "rejected": rejected_list})
jsave("data/seen.json", seen)
jsave("data/health.json", health)
jsave("data/rejects.json", rejects)
jsave("data/feedback_state.json", fb_state)

print(f"[boréale] {NOW} · examinées {len(collected)} · nouvelles {len(fresh)} · gardées {len(merged)} · "
      f"écartées {len(auto_rej)} · scoring {mode} · alertes {len(alerts)}")
if alerts:
    print("\n".join(" ! " + a for a in alerts))
    sys.exit(0)
