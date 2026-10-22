#!/usr/bin/env python3
"""
doctolib_watch.py : surveille les dispos Doctolib et envoie une notif push (ntfy) quand un creneau apparait.

Usage :
  python3 doctolib_watch.py --probe "<lien fiche Doctolib>"            # TEST : liste les motifs, verifie que Doctolib repond
  python3 doctolib_watch.py --probe "<lien fiche Doctolib>" molluscum  # TEST + dispos pour le motif contenant "molluscum"
  python3 doctolib_watch.py --test-notif                               # envoie une notif de test sur ton iPhone
  python3 doctolib_watch.py                                            # tourne en boucle, check toutes les ~10 min
  python3 doctolib_watch.py --once                                     # un seul check (cron / GitHub Actions)
  python3 doctolib_watch.py --report                                   # check immediat + notif de bilan
  python3 doctolib_watch.py --listen                                   # checks manuels via ntfy (topic <topic>-check)

Zero dependance : uniquement la lib standard Python 3.
"""

import json
import os
import random
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

# =============================== CONFIG ======================================

# Un bloc par medecin :
#   "booking_page" : le lien de la fiche Doctolib (copie depuis la barre d'adresse)
#   "motive"       : un mot du motif de RDV voulu (ex. "molluscum", "consultation")
#   "url"          : optionnel, une URL availabilities.json si tu l'as deja
# Peut etre remplace par la variable d'environnement DOCTORS_JSON (meme format, en JSON).
DOCTORS = []  # vide par defaut : configure via config.json ou la variable DOCTORS_JSON

# Ne notifier que pour les creneaux dans cette fenetre (format AAAA-MM-JJ).
NOT_BEFORE = os.environ.get("NOT_BEFORE", "2026-10-28")   # retour du Canada
NOT_AFTER = os.environ.get("NOT_AFTER", "2026-12-31")

# Notifs push : app "ntfy" sur iPhone, abonne-toi a ce topic.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

_cfg = Path(__file__).with_name("config.json")
if os.environ.get("DOCTORS_JSON"):
    DOCTORS = json.loads(os.environ["DOCTORS_JSON"])
elif _cfg.exists():
    _c = json.loads(_cfg.read_text())
    DOCTORS = _c.get("doctors", [])
    NTFY_TOPIC = NTFY_TOPIC or _c.get("ntfy_topic", "")

BASE = "https://www.doctolib.fr"

CHECK_EVERY_SECONDS = 600   # 10 minutes
JITTER_SECONDS = 60         # petit decalage aleatoire pour ne pas taper pile a l'heure

STATE_FILE = Path(__file__).with_name(".doctolib_watch_state.json")

# =============================================================================

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Accept": "application/json",
    "Accept-Language": "fr-FR,fr;q=0.9",
}


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def build_url(raw_url):
    """Force start_date a aujourd'hui (ou NOT_BEFORE si plus tard) pour toujours chercher a partir du bon jour."""
    parts = urllib.parse.urlsplit(raw_url)
    qs = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    start = max(date.today().isoformat(), NOT_BEFORE)
    qs["start_date"] = start
    qs.setdefault("limit", "7")
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(qs)))


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def profile_slug(booking_url):
    """https://www.doctolib.fr/dermatologue/paris/jean-dupont[/booking/...] -> jean-dupont"""
    segs = [s for s in urllib.parse.urlsplit(booking_url).path.split("/") if s]
    if "booking" in segs and segs.index("booking") > 0:
        return segs[segs.index("booking") - 1]
    for s in reversed(segs):
        if "-" in s and s not in ("booking", "availabilities.json"):
            return s
    raise ValueError(f"Impossible de lire le medecin dans ce lien : {booking_url}")


def fetch_info(booking_url):
    slug = profile_slug(booking_url)
    q = urllib.parse.urlencode({"profile_slug": slug, "locale": "fr"})
    data = fetch(f"{BASE}/online_booking/api/slot_selection_funnel/v1/info.json?{q}")
    return data.get("data", data)


def matching_motives(info, keyword):
    motives = info.get("visit_motives", []) or []
    if not keyword:
        return motives
    k = norm(keyword)
    return [m for m in motives if k in norm(m.get("name"))]


def availability_url(info, motive_id):
    agendas = [
        a for a in info.get("agendas", []) or []
        if not a.get("booking_disabled") and not a.get("booking_temporary_disabled")
        and motive_id in (a.get("visit_motive_ids") or [])
    ]
    if not agendas:
        return None
    agenda_ids = "-".join(dict.fromkeys(str(a["id"]) for a in agendas))
    practice_ids = [a.get("practice_id") for a in agendas if a.get("practice_id")]
    if not practice_ids:
        for p in info.get("places", []) or []:
            practice_ids += p.get("practice_ids") or []
    params = {
        "visit_motive_ids": motive_id,
        "agenda_ids": agenda_ids,
        "insurance_sector": "public",
        "telehealth": "false",
        "limit": "7",
    }
    if practice_ids:
        params["practice_ids"] = practice_ids[0]
    return f"{BASE}/availabilities.json?{urllib.parse.urlencode(params)}"


def resolve_url(doc, state):
    """URL availabilities.json du medecin : donnee directement, en cache, ou retrouvee depuis la fiche."""
    if doc.get("url"):
        return doc["url"]
    cache = state.setdefault("_resolved", {})
    if doc["name"] in cache:
        return cache[doc["name"]]
    info = fetch_info(doc["booking_page"])
    motives = matching_motives(info, doc.get("motive"))
    if not motives:
        names = ", ".join(m.get("name", "?") for m in info.get("visit_motives", []) or [])
        raise ValueError(f"Aucun motif ne contient '{doc.get('motive')}'. Motifs dispo : {names}")
    for m in motives:
        url = availability_url(info, m["id"])
        if url:
            log(f"{doc['name']} : motif retenu '{m.get('name')}'")
            cache[doc["name"]] = url
            return url
    raise ValueError("Motif trouve mais aucun agenda reservable en ligne pour ce motif.")


def extract_slots(data):
    """Renvoie la liste triee des creneaux (str ISO) presents dans la reponse."""
    slots = []
    for day in data.get("availabilities", []) or []:
        for s in day.get("slots", []) or []:
            if isinstance(s, str):
                slots.append(s)
            elif isinstance(s, dict) and s.get("start_date"):
                slots.append(s["start_date"])
    return sorted(slots)


def in_window(iso):
    d = iso[:10]
    return NOT_BEFORE <= d <= NOT_AFTER


def notify(title, message, click_url=None):
    if not NTFY_TOPIC:
        log("Pas de topic ntfy configure, notif ignoree.")
        return
    headers = {"Title": title.encode("utf-8"), "Priority": "high", "Tags": "stethoscope"}
    if click_url:
        headers["Click"] = click_url
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"), headers=headers, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        log(f"Echec de la notif ntfy : {e}")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        log(f"Impossible d'ecrire l'etat : {e}")


def fmt(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        jours = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]
        if len(iso) == 10:  # date seule (next_slot)
            return f"{jours[dt.weekday()]} {dt:%d/%m} (prochain jour dispo)"
        return f"{jours[dt.weekday()]} {dt:%d/%m %Hh%M}"
    except Exception:
        return iso


def check_doctor(doc, state):
    name = doc["name"]
    try:
        data = fetch(build_url(resolve_url(doc, state)))
    except ValueError as e:
        log(f"{name} : {e}")
        return False
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            log(f"{name} : Doctolib refuse la requete (HTTP {e.code}). Ralentis ou arrete le script.")
        else:
            log(f"{name} : erreur HTTP {e.code}")
        return False
    except Exception as e:
        log(f"{name} : erreur reseau {e}")
        return False

    slots = [s for s in extract_slots(data) if in_window(s)]
    # La requete ne couvre que 7 jours : si rien dedans mais que Doctolib annonce un
    # prochain creneau dans la fenetre, on le compte aussi.
    nxt = data.get("next_slot")
    if not slots and nxt and in_window(nxt):
        slots = [nxt]
    known = set(state.get(name, []))
    new = [s for s in slots if s not in known]

    if slots:
        log(f"{name} : {len(slots)} creneau(x) dans la fenetre, premier {fmt(slots[0])}")
    else:
        nxt = data.get("next_slot")
        log(f"{name} : rien dans la fenetre" + (f" (prochain dispo : {nxt})" if nxt else ""))

    if new:
        lines = "\n".join(fmt(s) for s in new[:6])
        more = f"\n+ {len(new) - 6} autre(s)" if len(new) > 6 else ""
        notify(f"Creneau dispo : {name}", lines + more, doc.get("booking_page"))
        log(f"{name} : notif envoyee pour {len(new)} nouveau(x) creneau(x)")

    # On ne garde que les creneaux encore visibles : si un creneau disparait puis revient, on re-notifie.
    state[name] = slots
    return True


def run_once():
    if not DOCTORS:
        log("Aucun medecin configure, rien a faire.")
        return True
    state = load_state()
    ok = False
    for i, doc in enumerate(DOCTORS):
        if i:
            time.sleep(random.uniform(3, 8))  # pas de rafale entre medecins
        ok = check_doctor(doc, state) or ok
    save_state(state)
    return ok


def report():
    """Check immediat + notif de bilan, meme s'il n'y a rien de nouveau."""
    state = load_state()
    lines = []
    for doc in DOCTORS:
        name = doc["name"]
        try:
            data = fetch(build_url(resolve_url(doc, state)))
        except Exception as e:
            lines.append(f"{name} : erreur ({e})")
            continue
        slots = [s for s in extract_slots(data) if in_window(s)]
        nxt = data.get("next_slot")
        if slots:
            lines.append(f"{name} : {len(slots)} creneau(x), premier {fmt(slots[0])}")
        elif nxt and in_window(nxt):
            lines.append(f"{name} : {fmt(nxt)}")
        else:
            lines.append(f"{name} : rien entre le {NOT_BEFORE[8:]}/{NOT_BEFORE[5:7]} et le {NOT_AFTER[8:]}/{NOT_AFTER[5:7]}")
    save_state(state)
    msg = "\n".join(lines) or "Aucun medecin configure."
    log("Bilan : " + msg.replace("\n", " | "))
    notify("Check manuel Doctolib", msg)


def listen(cmd_topic):
    """Ecoute un topic ntfy : chaque message recu declenche un check manuel."""
    url = f"https://ntfy.sh/{cmd_topic}/json"
    log(f"En ecoute sur ntfy.sh/{cmd_topic}")
    last = 0.0
    while True:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "doctolib-watch"}), timeout=90) as r:
                for raw in r:
                    try:
                        ev = json.loads(raw.decode("utf-8"))
                    except Exception:
                        continue
                    if ev.get("event") != "message":
                        continue
                    if time.time() - last < 60:  # anti-spam : 1 check manuel par minute max
                        notify("Check manuel Doctolib", "Deja fait il y a moins d'une minute, reessaie dans un instant.")
                        continue
                    last = time.time()
                    log("Check manuel demande")
                    report()
        except Exception as e:
            log(f"Ecoute interrompue ({e}), reconnexion dans 15 s")
            time.sleep(15)


def probe(booking_url, keyword=None):
    """Test manuel : Doctolib repond-il, quels motifs, et des dispos ?"""
    print(f"1) Lecture de la fiche ({profile_slug(booking_url)})...")
    try:
        info = fetch_info(booking_url)
    except urllib.error.HTTPError as e:
        print(f"   ECHEC : HTTP {e.code}." + (" Doctolib bloque les requetes automatiques, on s'arrete la." if e.code in (403, 429) else ""))
        return
    except Exception as e:
        print(f"   ECHEC : {e}")
        return
    motives = info.get("visit_motives", []) or []
    print(f"   OK, {len(motives)} motif(s) :")
    for m in motives:
        print(f"     - {m.get('name')}")
    if not keyword:
        print("\nRelance avec un mot du motif voulu, ex. : --probe \"<lien>\" molluscum")
        return
    picked = matching_motives(info, keyword)
    if not picked:
        print(f"\n   Aucun motif ne contient '{keyword}'.")
        return
    for m in picked:
        url = availability_url(info, m["id"])
        print(f"\n2) Dispos pour '{m.get('name')}'...")
        if not url:
            print("   Pas reservable en ligne pour ce motif.")
            continue
        try:
            data = fetch(build_url(url))
        except urllib.error.HTTPError as e:
            print(f"   ECHEC : HTTP {e.code}.")
            continue
        slots = extract_slots(data)
        print(f"   OK ! Doctolib repond. {len(slots)} creneau(x) sur 7 jours a partir du {max(date.today().isoformat(), NOT_BEFORE)}"
              + (f", premier : {fmt(slots[0])}" if slots else "")
              + (f" | prochain jour dispo : {data.get('next_slot')}" if data.get("next_slot") else ""))
        time.sleep(2)


def main():
    if "--probe" in sys.argv:
        i = sys.argv.index("--probe")
        args = sys.argv[i + 1:]
        if not args:
            print('Usage : --probe "<lien fiche Doctolib>" [mot du motif]')
            return
        probe(args[0], args[1] if len(args) > 1 else None)
        return
    if "--report" in sys.argv:
        report()
        return
    if "--listen" in sys.argv:
        if not NTFY_TOPIC:
            log("Pas de topic ntfy configure.")
            return
        listen(NTFY_TOPIC + "-check")
        return
    if "--test-notif" in sys.argv:
        notify("Test doctolib_watch", "Si tu vois ca, les notifs marchent.")
        log("Notif de test envoyee.")
        return
    if "--once" in sys.argv:
        time.sleep(random.uniform(0, JITTER_SECONDS))
        if not run_once():
            sys.exit(1)  # tout a echoue : le job GitHub passe en rouge et t'envoie un mail
        return
    log(f"Surveillance de {len(DOCTORS)} medecin(s), toutes les ~{CHECK_EVERY_SECONDS // 60} min. Ctrl+C pour arreter.")
    while True:
        run_once()
        time.sleep(CHECK_EVERY_SECONDS + random.uniform(0, JITTER_SECONDS))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Arret.")
