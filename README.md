# Sagra monitoring

Stack di monitoraggio per la sagra: exporter (scraping gestionale) → Prometheus → Grafana.
Gira in Docker su un portatile Linux nella stessa rete del gestionale.

## Configurazione

Prima del primo avvio, copia i due file di esempio e adattali:

```sh
cp exporter.env.example exporter.env   # URL gestionale, credenziali basic auth, porte
cp limits.env.example limits.env       # scorte/soglie di alert (modificate ogni giorno)
```

## Workflow

```sh
make setup     # una tantum, con internet: installa i tool di formatting (ruff + prettier)
make pull      # scarica le immagini Docker per l'arch del portatile
make save      # salva le immagini in un tar (per il trasferimento airgapped)
make load      # sul portatile airgapped: carica le immagini dal tar
make up        # interpola i limiti (render), avvia lo stack e ricarica gli alert Grafana
make reload    # riapplica limits.env aggiornato senza fermare exporter/prometheus
make firewall  # apre la porta Grafana in LAN
make down      # ferma lo stack
```

I limiti di alert giornalieri si impostano in `limits.env`; `make up` li interpola nei file
Grafana. Dopo averli modificati, `make fmt` pulisce e formatta tutto (python, yaml, json, env),
`make lint` verifica senza modificare.

`make help` mostra tutti i target.

## Nota — aggiornamento serale dei limiti

Le scorte e le soglie di alert si aggiornano ogni sera in `limits.env`. Per applicarle **a stack
già avviato** usa:

```sh
make reload
```

`make reload` rigenera i file da `limits.env` e ricarica **solo Grafana** (~pochi secondi), senza
fermare exporter e Prometheus: nessun buco nei dati raccolti.

Perché serve un comando dedicato:

- **Gauge e dashboard** leggono le soglie dai file dashboard, che Grafana ricarica **da sole** ogni
  30 secondi: nessun riavvio necessario.
- **Gli alert**, invece, vengono letti dal provisioning **solo all'avvio del container Grafana**. Un
  semplice `docker compose up -d` a stack acceso **non** li aggiorna: per questo `make reload` (come
  anche `make up` e `make restart`) forza il riavvio di Grafana.
- Le recording rule del consumo BOM sono **Grafana-managed**: le valuta Grafana e
  ne scrive il risultato su Prometheus via remote-write. Non dipendono da
  `limits.env`, quindi non vanno toccate: basta ricaricare Grafana.

## Nota — visibilità degli allarmi (nessun servizio di notifica)

Il notebook è in airgap, quindi **non** c'è invio di notifiche (email/webhook/toast).
Gli allarmi si vedono **direttamente nelle dashboard**:

- **Pannello «Allarmi attivi — ingredienti in riserva»** nella dashboard _Gestore_ (recap
  accentratore): elenca in tempo reale gli allarmi in stato _firing_/_pending_. Se è vuoto,
  tutte le scorte sono sopra soglia.
- **Ogni allarme è collegato al proprio gauge** (annotazioni `__dashboardUid__`/`__panelId__`):
  Lasagne/Tortelli → gauge _Primi_, Salsiccia/Spiedini/Fritto/Tigelle → gauge _Secondi_. Il
  gauge diventa **rosso** al superamento della soglia. Gli allarmi non fanno _flapping_: una
  volta in riserva ci si resta, quindi il gauge rosso + il pannello elenco bastano a segnalare.
