"""
Exporter Prometheus per il gestionale sagra (airgapped).

Un thread di refresh in background interroga l'endpoint AJAX del gestionale
(Basic Auth) ogni REFRESH_INTERVAL secondi (default 10s), ne estrae la tabella
articoli venduti e ne aggiorna uno snapshot in cache. Ad ogni scrape Prometheus
serve lo snapshot corrente come metriche 1:1 (nessuna logica BOM qui:
frazioni/soglie vivono in Grafana). Cosi' la cadenza di aggiornamento dei dati
non dipende dalla frequenza di scrape e lo scrape resta immediato.

Endpoint sorgente:
  GET {BASE}{AJAX_PATH}?akcsv=...&tmin1=..&tmin2=..&_=<epoch_ms>
  -> JSON { "selling_grid_html": "<table>...", "selling_epoch": "...", "foo": "1" }
"""

import logging
import os
import re
import threading
import time
from html.parser import HTMLParser

import requests
from prometheus_client import start_http_server
from prometheus_client.core import REGISTRY, GaugeMetricFamily
from requests.auth import HTTPBasicAuth

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("sagra-exporter")


# ---------------------------------------------------------------------------
# Config (via env)
# ---------------------------------------------------------------------------
def _env(name, default):
    return os.environ.get(name, default)


BASE = _env("GESTIONALE_BASE", "http://192.168.1.11:8073").rstrip("/")
AJAX_PATH = _env("GESTIONALE_AJAX_PATH", "/realtime/selling_ajax_reload.jssp")
AKCSV = _env("GESTIONALE_AKCSV", "0,1,5,9,7,3,4,8,16,6,22,23,24")
TMIN1 = _env("GESTIONALE_TMIN1", "5")
TMIN2 = _env("GESTIONALE_TMIN2", "20")
USER = _env("GESTIONALE_USER", "admin")
PASS = _env("GESTIONALE_PASS", "12345")
AUTH_MODE = _env("GESTIONALE_AUTH", "basic").lower()  # basic | digest | none
TIMEOUT = float(_env("SCRAPE_TIMEOUT", "8"))
PORT = int(_env("EXPORTER_PORT", "8000"))
# Ogni quanti secondi il thread di background rilegge il gestionale.
REFRESH_INTERVAL = float(_env("REFRESH_INTERVAL", "10"))

# Mappa: testo header nella tabella -> nome metrica Prometheus.
# I nomi header sono normalizzati (lower, spazi singoli) prima del confronto.
# L'unica metrica significativa e' "Qt tot" (totale cumulativo venduto).
HEADER_TO_METRIC = {
    "qt tot": ("sagra_qt_totale", "Totale cumulativo venduto per articolo"),
}


# ---------------------------------------------------------------------------
# Parser tabella
# ---------------------------------------------------------------------------
class _GridParser(HTMLParser):
    """Estrae header, celle testuali e id articolo (dal select onchange).

    L'id articolo e' contenuto nel <select> della colonna Disponibilita
    (onchange="selling_ajax_qt(this,ID)"): lo parsiamo solo per l'id, la
    colonna Disponibilita in se' non viene esportata.
    """

    def __init__(self):
        super().__init__()
        self.rows = []  # lista di righe; ogni riga = list[str] (celle)
        self.row_ids = []  # id articolo per riga (dal select onchange)
        self._cur = None
        self._buf = None
        self._id = None
        self._in_cell = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self._cur = []
            self._id = None
        elif tag in ("td", "th"):
            self._buf = []
            self._in_cell = True
        elif tag == "select":
            m = re.search(r"selling_ajax_qt\(this,\s*(\d+)\)", a.get("onchange", "") or "")
            if m:
                self._id = m.group(1)

    def handle_data(self, data):
        if self._in_cell and self._buf is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._in_cell:
            self._cur.append("".join(self._buf).strip())
            self._in_cell = False
            self._buf = None
        elif tag == "tr" and self._cur is not None:
            self.rows.append(self._cur)
            self.row_ids.append(self._id)
            self._cur = None


def _norm(s):
    s = s.replace("\u00e0", "a").replace("&agrave;", "a")  # Disponibilità -> disponibilita
    return re.sub(r"\s+", " ", s).strip().lower()


def parse_grid(grid_html):
    """Ritorna (header_norm: list[str], records: list[dict]).

    Ogni record: {'articolo': str, 'id': str|None, '<metric>': float, ...}
    Mappa le colonne per NOME header (robusto ai cambi di akcsv/ordine).
    """
    p = _GridParser()
    p.feed(grid_html)
    if not p.rows:
        return [], []

    header = [_norm(h) for h in p.rows[0]]
    # indice della colonna "Articolo" (nome piatto)
    try:
        name_idx = header.index("articolo")
    except ValueError:
        name_idx = None

    records = []
    for cells, art_id in zip(p.rows[1:], p.row_ids[1:], strict=False):
        if not cells:
            continue
        rec = {"id": art_id}
        rec["articolo"] = cells[name_idx] if name_idx is not None and name_idx < len(cells) else ""
        for idx, col in enumerate(header):
            if col in HEADER_TO_METRIC and idx < len(cells):
                metric_name = HEADER_TO_METRIC[col][0]
                raw = cells[idx].strip()
                try:
                    rec[metric_name] = float(raw)
                except (ValueError, TypeError):
                    pass  # cella non numerica -> salta questa metrica
        records.append(rec)
    return header, records


# ---------------------------------------------------------------------------
# Collector Prometheus (serve uno snapshot aggiornato in background)
# ---------------------------------------------------------------------------
class SagraCollector:
    def __init__(self):
        self._session = requests.Session()
        if AUTH_MODE == "basic":
            self._session.auth = HTTPBasicAuth(USER, PASS)
        elif AUTH_MODE == "digest":
            from requests.auth import HTTPDigestAuth

            self._session.auth = HTTPDigestAuth(USER, PASS)
        # AUTH_MODE == "none" -> nessuna auth
        self._lock = threading.Lock()
        self._snapshot = {"records": [], "success": 0, "epoch": None, "duration": 0.0}

    def _fetch(self):
        params = {
            "akcsv": AKCSV,
            "tmin1": TMIN1,
            "tmin2": TMIN2,
            "_": str(int(time.time() * 1000)),  # anti-cache
        }
        r = self._session.get(
            BASE + AJAX_PATH,
            params=params,
            timeout=TIMEOUT,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        r.raise_for_status()
        return r.json()

    def refresh(self):
        """Interroga il gestionale e aggiorna lo snapshot in cache."""
        started = time.time()
        success = 1
        epoch_val = None
        records = []
        try:
            data = self._fetch()
            epoch_val = data.get("selling_epoch")
            _, records = parse_grid(data.get("selling_grid_html", ""))
        except Exception as exc:  # noqa: BLE001
            success = 0
            log.warning("refresh fallito: %s", exc)
        with self._lock:
            self._snapshot = {
                "records": records,
                "success": success,
                "epoch": epoch_val,
                "duration": time.time() - started,
            }

    def _run_loop(self):
        while True:
            try:
                self.refresh()
            except Exception as exc:  # noqa: BLE001 (il loop non deve mai morire)
                log.error("errore imprevisto nel loop di refresh: %s", exc)
            time.sleep(REFRESH_INTERVAL)

    def start(self):
        """Primo refresh sincrono + avvio del thread di background."""
        self.refresh()
        threading.Thread(target=self._run_loop, name="refresh", daemon=True).start()

    def collect(self):
        with self._lock:
            snap = self._snapshot
        records = snap["records"]

        # Una GaugeMetricFamily per metrica, label: articolo + id
        families = {}
        for _, (metric_name, help_text) in HEADER_TO_METRIC.items():
            families[metric_name] = GaugeMetricFamily(
                metric_name, help_text, labels=["articolo", "id"]
            )

        for rec in records:
            labels = [rec.get("articolo", ""), rec.get("id") or ""]
            for metric_name, fam in families.items():
                if metric_name in rec:
                    fam.add_metric(labels, rec[metric_name])

        for fam in families.values():
            yield fam

        # Metriche di servizio/health (riferite all'ultimo refresh)
        yield GaugeMetricFamily(
            "sagra_scrape_success",
            "1 se l'ultimo refresh del gestionale e' riuscito, 0 altrimenti",
            value=snap["success"],
        )
        yield GaugeMetricFamily(
            "sagra_articoli",
            "Numero di articoli letti dalla tabella",
            value=len(records),
        )
        yield GaugeMetricFamily(
            "sagra_scrape_duration_seconds",
            "Durata dell'ultimo refresh del gestionale",
            value=snap["duration"],
        )
        if snap["epoch"] is not None:
            try:
                yield GaugeMetricFamily(
                    "sagra_dati_epoch",
                    "Timestamp (selling_epoch) dei dati serviti dal gestionale",
                    value=float(snap["epoch"]),
                )
            except (ValueError, TypeError):
                pass


def main():
    log.info(
        "Exporter sagra su :%d -> %s%s (auth=%s, refresh=%.0fs)",
        PORT,
        BASE,
        AJAX_PATH,
        AUTH_MODE,
        REFRESH_INTERVAL,
    )
    collector = SagraCollector()
    collector.start()
    REGISTRY.register(collector)
    start_http_server(PORT)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
