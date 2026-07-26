#!/usr/bin/env python3
"""Normalizzatore per file .env (stdlib, nessuna dipendenza).

Pulisce i file .env cosi' che gli edit giornalieri dei limiti di alert
restino ordinati e senza errori tipici:
  - rimuove spazi finali e indentazione superflua
  - normalizza `KEY = valore` -> `KEY=valore`
  - rimuove un eventuale prefisso `export ` (incompatibile con env_file di compose)
  - collassa righe vuote multiple in una sola
  - deduplica le chiavi mantenendo l'ultimo valore (con avviso)
  - garantisce una singola newline finale
  - valida che le chiavi siano identificatori validi ([A-Za-z_][A-Za-z0-9_]*)

Uso:
  python3 tools/fmt_env.py FILE...            # riscrive in-place
  python3 tools/fmt_env.py --check FILE...     # esce !=0 se servono modifiche
"""

import argparse
import re
import sys

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def normalize(text):
    """Ritorna (nuovo_testo, warnings)."""
    warnings = []
    raw_lines = text.split("\n")
    # split("\n") su testo con newline finale produce un "" in coda: lo togliamo
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()

    out = []
    seen = {}  # key -> indice in out
    blank_pending = False

    for lineno, line in enumerate(raw_lines, 1):
        stripped = line.strip()

        if stripped == "":
            blank_pending = True
            continue

        if stripped.startswith("#"):
            if blank_pending and out:
                out.append("")
            blank_pending = False
            out.append(stripped.rstrip())
            continue

        # riga di assegnazione
        body = stripped
        if body.startswith("export "):
            body = body[len("export ") :].lstrip()

        if "=" not in body:
            warnings.append(f"riga {lineno}: assegnazione non valida (manca '='): {stripped!r}")
            if blank_pending and out:
                out.append("")
            blank_pending = False
            out.append(stripped.rstrip())
            continue

        key, value = body.split("=", 1)
        key = key.strip()
        value = value.strip()

        if not KEY_RE.match(key):
            warnings.append(f"riga {lineno}: chiave non valida {key!r}")

        if blank_pending and out:
            out.append("")
        blank_pending = False

        new_line = f"{key}={value}"
        if key in seen:
            warnings.append(f"chiave duplicata {key!r}: mantengo l'ultimo valore")
            out[seen[key]] = new_line
        else:
            seen[key] = len(out)
            out.append(new_line)

    return "\n".join(out) + "\n", warnings


def process(path, check):
    try:
        with open(path, encoding="utf-8") as f:
            original = f.read()
    except FileNotFoundError:
        print(f"  salto (inesistente): {path}", file=sys.stderr)
        return True  # non e' un errore bloccante

    new, warnings = normalize(original)
    for w in warnings:
        print(f"  {path}: {w}", file=sys.stderr)

    if new == original:
        return True

    if check:
        print(f"  DA FORMATTARE: {path}", file=sys.stderr)
        return False

    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    print(f"  formattato: {path}")
    return True


def main():
    ap = argparse.ArgumentParser(description="Normalizza file .env")
    ap.add_argument("files", nargs="*", help="file .env da processare")
    ap.add_argument(
        "--check", action="store_true", help="non modifica; esce !=0 se servono modifiche"
    )
    args = ap.parse_args()

    ok = True
    for path in args.files:
        ok = process(path, args.check) and ok
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
