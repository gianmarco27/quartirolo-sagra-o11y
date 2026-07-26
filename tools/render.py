#!/usr/bin/env python3
"""Render dei template Grafana interpolando i limiti da limits.env (stdlib).

Perche' esiste: Grafana NON interpola le variabili d'ambiente dentro le
espressioni delle alert rule provisionate. Quindi le soglie non possono
arrivare "a runtime" dall'ambiente: le interpoliamo noi qui, a monte, in
modo uniforme sia per gli YAML di alerting sia per i JSON delle dashboard.

Meccanica:
  - legge le variabili da limits.env (KEY=VALUE)
  - per ogni file sotto grafana/templates/ con estensione .tmpl, sostituisce
    i segnaposto %%VAR%% e scrive l'output nel percorso speculare sotto
    grafana/ (senza .tmpl). Es.:
      grafana/templates/provisioning/alerting/reg.yaml.tmpl
        -> grafana/provisioning/alerting/reg.yaml
      grafana/templates/dashboards/board.json.tmpl
        -> grafana/dashboards/board.json

Il segnaposto e' %%VAR%% (e non ${VAR}) apposta: i file Grafana usano gia' '$'
per i refId delle alert ($A, $B) e per le variabili delle dashboard
($__rate_interval, ...); un token con '$' colliderebbe con questi.

Uso:
  python3 tools/render.py [--limits limits.env] [--check]
"""

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(ROOT, "grafana", "templates")
OUTPUT_ROOT = os.path.join(ROOT, "grafana")
TOKEN = re.compile(r"%%([A-Za-z0-9_]+)%%")


def load_env(path):
    values = {}
    if not os.path.exists(path):
        print(f"attenzione: {path} inesistente (nessuna variabile caricata)", file=sys.stderr)
        return values
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            if s.startswith("export "):
                s = s[len("export ") :].lstrip()
            k, v = s.split("=", 1)
            values[k.strip()] = v.strip()
    return values


def iter_templates():
    if not os.path.isdir(TEMPLATES_DIR):
        return
    for dirpath, _dirs, files in os.walk(TEMPLATES_DIR):
        for name in files:
            if name.endswith(".tmpl"):
                yield os.path.join(dirpath, name)


def output_path(tmpl_path):
    rel = os.path.relpath(tmpl_path, TEMPLATES_DIR)
    rel = rel[: -len(".tmpl")]  # rimuove il suffisso .tmpl
    return os.path.join(OUTPUT_ROOT, rel)


def render_one(tmpl_path, values, check):
    with open(tmpl_path, encoding="utf-8") as f:
        template = f.read()

    missing = []

    def _sub(m):
        key = m.group(1)
        if key not in values:
            missing.append(key)
            return m.group(0)
        return values[key]

    rendered = TOKEN.sub(_sub, template)
    if missing:
        uniq = ", ".join(sorted(set(missing)))
        print(f"ERRORE {tmpl_path}: variabili mancanti in limits.env: {uniq}", file=sys.stderr)
        return False

    out = output_path(tmpl_path)
    existing = None
    if os.path.exists(out):
        with open(out, encoding="utf-8") as f:
            existing = f.read()

    if existing == rendered:
        return True

    if check:
        print(f"  DA RIGENERARE: {out}", file=sys.stderr)
        return False

    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(rendered)
    print(f"  generato: {os.path.relpath(out, ROOT)}")
    return True


def main():
    ap = argparse.ArgumentParser(description="Render template Grafana da limits.env")
    ap.add_argument("--limits", default=os.path.join(ROOT, "limits.env"))
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    values = load_env(args.limits)
    templates = list(iter_templates())
    if not templates:
        print("nessun template in grafana/templates/ (niente da fare)")
        return

    ok = True
    for tmpl in templates:
        ok = render_one(tmpl, values, args.check) and ok
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
