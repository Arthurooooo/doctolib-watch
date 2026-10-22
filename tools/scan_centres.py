#!/usr/bin/env python3
"""Scan des etablissements conventionnes (dermato) : description de la fiche + premieres dispos."""
import json, re, sys, time, unicodedata, urllib.parse, urllib.request

BASE = "https://www.doctolib.fr"
START = "2026-10-28"
H = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Accept-Language": "fr-FR,fr;q=0.9",
}


def get(url, as_json=True):
    req = urllib.request.Request(url, headers={**H, "Accept": "application/json" if as_json else "text/html"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw) if as_json else raw


def norm(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c)).lower()


def walk(x, out):
    if isinstance(x, list):
        for y in x:
            walk(y, out)
    elif isinstance(x, dict):
        out.append(x)
        for v in x.values():
            walk(v, out)


def fiche(link):
    html = get(BASE + link, as_json=False)
    objs = []
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            walk(json.loads(m.group(1)), objs)
        except Exception:
            pass
    desc = next((o["description"] for o in objs if isinstance(o.get("description"), str) and len(o["description"]) > 40), "")
    offers = []
    for o in objs:
        if o.get("@type") in ("Offer", "AggregateOffer"):
            offers.append({k: o.get(k) for k in ("name", "description", "price", "lowPrice", "highPrice") if o.get(k)})
    return re.sub(r"\s+", " ", desc).strip(), offers


def dispo(link):
    slug = link.split("?")[0].rstrip("/").split("/")[-1]
    d = get(f"{BASE}/online_booking/api/slot_selection_funnel/v1/info.json?" + urllib.parse.urlencode({"profile_slug": slug, "locale": "fr"}))
    d = d.get("data", d)
    motives = d.get("visit_motives", []) or []
    derm = [m for m in motives if "dermato" in norm(m.get("name"))]
    pick = (next((m for m in derm if "suivi" in norm(m["name"])), None)
            or next((m for m in derm if re.search(r"premi|1e|1ere|nouveau", norm(m["name"]))), None)
            or (derm[0] if derm else None))
    res = {"motifs_dermato": [m["name"] for m in derm][:8]}
    if not pick:
        res["statut"] = "pas de motif dermato reservable"
        return res
    res["motif"] = pick["name"]
    ags = [a for a in d.get("agendas", []) or [] if not a.get("booking_disabled") and not a.get("booking_temporary_disabled")
           and pick["id"] in (a.get("visit_motive_ids") or [])]
    if not ags:
        res["statut"] = "pas d'agenda en ligne"
        return res
    practice = next((a["practice_id"] for a in ags if a.get("practice_id")), None)
    time.sleep(1.5)
    q = {"visit_motive_ids": pick["id"], "agenda_ids": "-".join(str(a["id"]) for a in ags),
         "insurance_sector": "public", "telehealth": "false", "limit": "7", "start_date": START}
    if practice:
        q["practice_ids"] = practice
    av = get(f"{BASE}/availabilities.json?" + urllib.parse.urlencode(q))
    slots = [s if isinstance(s, str) else s.get("start_date") for day in av.get("availabilities", []) or [] for s in day.get("slots", []) or []]
    res["premier_creneau"] = slots[0] if slots else None
    res["creneaux_7j"] = len(slots)
    res["prochain"] = av.get("next_slot")
    res["raison"] = av.get("reason")
    return res


def main():
    centres = json.load(open(sys.argv[1]))
    out = []
    for c in centres:
        r = {"nom": c["n"].strip(), "cp": c["z"], "lien": BASE + c["l"]}
        try:
            r["description"], r["tarifs"] = fiche(c["l"])
        except Exception as e:
            r["err_fiche"] = str(e)
        time.sleep(1.5)
        try:
            r.update(dispo(c["l"]))
        except Exception as e:
            r["err_dispo"] = str(e)
        out.append(r)
        print(json.dumps(r, ensure_ascii=False)[:200], flush=True)
        json.dump(out, open("scan_result.json", "w"), ensure_ascii=False, indent=1)
        time.sleep(1.5)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
