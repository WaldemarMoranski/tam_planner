#!/usr/bin/env python3
"""TAM Planner sync: Jira (board 171, future sprints) -> index.html rawData.

Usage:
  python3 scripts/sync_planner.py --baseline index.html --jira jira.json \
      [--overrides overrides.json] [--names names.json] [--epics epics.json] \
      --out index.html --summary summary.md

jira.json   = raw JSON from Jira search (`issues` list) for
              `project = TM AND sprint in futureSprints()` with fields
              summary,status,issuetype,timeoriginalestimate,parent,customfield_10020
names.json  = {"TM-2488": "English task name"} – translations made by the agent
epics.json  = raw JSON from Jira search for the parent epics (`key in (...)`) with
              fields summary,customfield_11258 (epic "Client" field)
Client priority: overrides.json > epic Client field > previous Planner > [Prefix] > "?"
overrides.json = {
   "hours":  {"2026_Q4_S5": {"TM-2680": 4}},   # manual hours win over Jira
   "client": {"TM-2825": "CTP"},               # manual client mapping
   "bugfix_hours": 10, "default_cap": 180, "default_prol": 0
}
Only the rawData object inside index.html is changed; the viewer code stays as is.
"""
import argparse, json, re, time, datetime, collections

SPRINT_FIELD = "customfield_10020"
CLIENT_FIELD = "customfield_11258"
PART_TYPES = {"Frontend": "FE", "Backend/API": "BE", "Mobile": "Mobile"}
SUFFIX_ORDER = ["FE", "BE", "Mobile"]
BUG_TYPES = {"Bug"}
SKIP_STATUS = {"Canceled", "Cancelled"}
POLISH = re.compile(r"[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]|\b(?:Integracja|Analiza|zapisu|wymagane|przez|najemców|dla|oraz)\b")


def load_raw(html):
    i = html.find("rawData={")
    if i < 0:
        raise SystemExit("rawData not found in baseline html")
    start = i + len("rawData=")
    obj, end = json.JSONDecoder().raw_decode(html[start:])
    return obj, start, start + end


def sprint_of(fields):
    sps = [s for s in (fields.get(SPRINT_FIELD) or []) if s.get("state") == "future"]
    if not sps:
        sps = fields.get(SPRINT_FIELD) or []
    return sps[-1]["name"] if sps else None


def strip_suffix(name):
    return re.sub(r"\s*\((?:FE|BE|Mobile)(?:\+(?:FE|BE|Mobile))*\)\s*$", "", name or "").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--jira", required=True)
    ap.add_argument("--overrides")
    ap.add_argument("--names")
    ap.add_argument("--epics")
    ap.add_argument("--out", required=True)
    ap.add_argument("--summary", required=True)
    a = ap.parse_args()

    html = open(a.baseline, encoding="utf-8").read()
    raw, s0, s1 = load_raw(html)
    jira = json.load(open(a.jira, encoding="utf-8"))
    issues = jira["issues"] if isinstance(jira, dict) else jira
    ov_file = json.load(open(a.overrides, encoding="utf-8")) if a.overrides else {}
    names = json.load(open(a.names, encoding="utf-8")) if a.names else {}
    man_hours = ov_file.get("hours", {})
    man_client = ov_file.get("client", {})
    bug_h = ov_file.get("bugfix_hours", 10)
    dcap, dprol = ov_file.get("default_cap", 180), ov_file.get("default_prol", 0)

    epic_client_raw = {}
    if a.epics:
        ej = json.load(open(a.epics, encoding="utf-8"))
        for e in (ej["issues"] if isinstance(ej, dict) else ej):
            v = (e.get("fields") or {}).get(CLIENT_FIELD)
            if isinstance(v, list):
                v = v[0] if v else None
            if isinstance(v, dict):
                v = v.get("value")
            if isinstance(v, str) and v.strip():
                epic_client_raw[e["key"]] = re.sub(r"\s+", " ", v).strip()

    # knowledge from baseline: names, clients, ids per Jira key
    base_name, base_client, base_id, base_pos = {}, {}, {}, {}
    for sp, tl in raw.get("tasks", {}).items():
        for t in tl:
            k = t.get("jira")
            if k and k != "-":
                base_name[k] = t.get("name")
                if t.get("client") not in (None, "", "-", "?"):
                    base_client[k] = t["client"]
                base_id[(sp, k)] = t.get("id")
                base_pos.setdefault(k, set()).add(sp)

    # map Jira "Client" values onto client names already used in the Planner
    known = {c for c in list(base_client.values()) + list(man_client.values()) if c}
    def canon(v):
        for c in known:
            if c.lower() == v.lower():
                return c
        first = v.split()[0].lower()
        for c in known:
            if c.lower() == first:
                return c
        return v
    epic_client = {k: canon(v) for k, v in epic_client_raw.items()}

    groups = collections.OrderedDict()   # (sprint, key) -> info
    flags = []
    for it in issues:
        f = it["fields"]
        st = f["status"]["name"]
        if st in SKIP_STATUS:
            continue
        sp = sprint_of(f)
        if not sp:
            continue
        itype = f["issuetype"]["name"]
        if itype in BUG_TYPES:
            continue  # covered by the single "Bugfixes" line
        est = (f.get("timeoriginalestimate") or 0) / 3600.0
        parent = f.get("parent")
        if parent and itype in PART_TYPES:
            key = parent["key"]
            epic_summary = (parent.get("fields") or {}).get("summary") or f["summary"]
        else:
            key = it["key"]
            epic_summary = f["summary"]
        g = groups.setdefault((sp, key), {"h": 0.0, "parts": set(), "summary": epic_summary, "noest": []})
        g["h"] += est
        if itype in PART_TYPES:
            g["parts"].add(PART_TYPES[itype])
        if not est:
            g["noest"].append(it["key"])

    synced_sprints = sorted({sp for sp, _ in groups})
    new_tasks = {sp: [] for sp in synced_sprints}
    now_ms = int(time.time() * 1000)
    for n, ((sp, key), g) in enumerate(groups.items()):
        if g["h"] == 0 and key not in man_hours.get(sp, {}):
            flags.append(f"{key} ({sp}): brak estymaty w Jirze – pominięte ({', '.join(g['noest'])})")
            continue
        suffix = "+".join(p for p in SUFFIX_ORDER if p in g["parts"])
        if key in names:
            name = names[key]
        elif key in base_name:
            name = base_name[key]
        else:
            name = g["summary"] + (f" ({suffix})" if suffix else "")
            flags.append(f"{key}: nowa nazwa z Jiry – sprawdź tłumaczenie: „{name}”")
        if POLISH.search(name):
            flags.append(f"{key}: nazwa wygląda na polską – dodaj tłumaczenie do names.json: „{name}”")
        client = man_client.get(key) or epic_client.get(key) or base_client.get(key)
        if a.epics and key not in man_client and key not in epic_client:
            flags.append(f"{key}: brak pola Client w epiku w Jirze")
        if not client:
            m = re.match(r"\[([^\]]+)\]", g["summary"])
            client = m.group(1) if m and not m.group(1).lower().startswith(("analiza", "f]", "b]", "m]")) else "?"
            flags.append(f"{key}: klient ustalony automatycznie = „{client}” – potwierdź")
        hours = man_hours.get(sp, {}).get(key)
        if hours is not None and abs(hours - g["h"]) > 0.01:
            flags.append(f"{key} ({sp}): ręczna korekta {hours}h zachowana (Jira: {round(g['h'],1)}h)")
        h = hours if hours is not None else round(g["h"], 1)
        if float(h).is_integer():
            h = int(h)
        new_tasks[sp].append({
            "name": name, "client": client, "jira": key,
            "url": f"https://singu.atlassian.net/browse/{key}",
            "type": "client" if client == "Prologis" else "feature",
            "h": h, "id": base_id.get((sp, key)) or now_ms + n,
        })

    # keep manual non-Jira items (e.g. "potential") from baseline, then one Bugfixes line
    for sp in synced_sprints:
        for t in raw.get("tasks", {}).get(sp, []):
            if t.get("type") == "potential":
                new_tasks[sp].append(t)
        new_tasks[sp].append({"name": "Bugfixes", "client": "-", "jira": "", "url": "",
                              "type": "bugfix", "h": bug_h, "id": now_ms + 9000 + synced_sprints.index(sp)})

    # diff vs baseline
    old = raw.get("tasks", {})
    diff = []
    for sp in synced_sprints:
        ob = {t.get("jira"): t for t in old.get(sp, []) if t.get("jira")}
        nb = {t.get("jira"): t for t in new_tasks[sp] if t.get("jira")}
        for k in nb.keys() - ob.keys():
            frm = [s for s in base_pos.get(k, ()) if s != sp]
            diff.append(f"{sp}: ➕ {k} {nb[k]['name']} ({nb[k]['h']}h)" + (f" – przeniesione z {', '.join(sorted(frm))}" if frm else ""))
        for k in ob.keys() - nb.keys():
            diff.append(f"{sp}: ➖ {k} {ob[k]['name']} ({ob[k]['h']}h)")
        for k in ob.keys() & nb.keys():
            if ob[k].get("h") != nb[k]["h"]:
                diff.append(f"{sp}: ✏️ {k} {ob[k].get('h')}h → {nb[k]['h']}h")

    for sp in synced_sprints:
        old[sp] = new_tasks[sp]
        raw.setdefault("ov", {}).setdefault(sp, {"cap": dcap, "prol": dprol})
        raw.setdefault("vis", {})[sp] = True
    raw["tasks"] = old
    raw["exportedAt"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    out_html = html[:s0] + json.dumps(raw, ensure_ascii=False, separators=(",", ":")) + html[s1:]
    open(a.out, "w", encoding="utf-8").write(out_html)

    lines = ["## Capacity"]
    for sp in synced_sprints:
        o = raw["ov"].get(sp, {})
        cap = o.get("cap", raw.get("cap", dcap))
        prol = o.get("prol", raw.get("prol", 0))
        tot = sum(t["h"] for t in new_tasks[sp]) + prol
        mark = "🔴" if tot > cap else "🟢"
        lines.append(f"- {mark} {sp}: {round(tot,1)}h / {cap}h ({'+' if cap-tot>=0 else ''}{round(cap-tot,1)}h)")
    lines.append("\n## Zmiany względem poprzedniej wersji")
    lines += [f"- {d}" for d in diff] or ["- brak"]
    lines.append("\n## Do sprawdzenia")
    lines += [f"- {fl}" for fl in flags] or ["- brak"]
    open(a.summary, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
