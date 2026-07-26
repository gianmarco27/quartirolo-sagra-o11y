# Makefile sagra — stack di monitoraggio (Prometheus + Grafana + exporter)
# Uso: make <target>. `make help` per l'elenco.

DC        := docker compose
PY        ?= python3
# PLATFORM: arch del portatile target (per l'airgap)
PLATFORM  ?= linux/amd64
GRAFANA_PORT ?= 3000
TAR       ?= sagra-images.tar
IMAGES    := prom/prometheus:v3.13.1 grafana/grafana:13.1.1
EXP_IMG   := sagra-exporter:local
ENV_FILES := $(wildcard exporter.env exporter.env.example limits.env limits.env.example)

.DEFAULT_GOAL := help
.PHONY: help up down restart reload logs ps build pull save load firewall setup fmt lint render test-e2e

help: ## Mostra questo aiuto
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | sort

up: render ## Avvia lo stack e applica il provisioning Grafana (alert da limits.env)
	$(DC) up -d
	@echo "Ricarico Grafana per applicare gli alert da limits.env..."
	$(DC) restart grafana

reload: render ## Applica limits.env aggiornato senza fermare exporter/prometheus (ricarica solo Grafana)
	$(DC) up -d
	@echo "Rigenerati i file da limits.env, ricarico gli alert Grafana (le dashboard si aggiornano da sole)..."
	$(DC) restart grafana

down: ## Ferma e rimuove i container
	$(DC) down

restart: down up ## Riavvia lo stack

logs: ## Segui i log
	$(DC) logs -f

ps: ## Stato dei container
	$(DC) ps

build: ## (Re)builda l'immagine exporter
	$(DC) build exporter

pull: ## Scarica le immagini base per l'arch PLATFORM
	@for img in $(IMAGES); do docker pull --platform $(PLATFORM) $$img; done

save: build ## Salva tutte le immagini nel tar per il trasferimento airgap
	docker save $(IMAGES) $(EXP_IMG) -o $(TAR)
	@echo "Creato $(TAR) — copialo sul portatile e usa 'make load'"

load: ## Carica le immagini dal tar sul portatile airgapped
	docker load -i $(TAR)

firewall: ## Apri la porta Grafana in LAN (GRAFANA_PORT/tcp)
	@if command -v ufw >/dev/null 2>&1; then \
		sudo ufw allow $(GRAFANA_PORT)/tcp; \
	elif command -v firewall-cmd >/dev/null 2>&1; then \
		sudo firewall-cmd --permanent --add-port=$(GRAFANA_PORT)/tcp && sudo firewall-cmd --reload; \
	else \
		echo "ufw/firewalld non trovati: apri manualmente la porta $(GRAFANA_PORT)/tcp"; \
	fi

setup: ## Installa i tool di formatting (una tantum, con internet, prima dell'airgap)
	sudo apt-get update
	sudo apt-get install -y python3-venv nodejs npm
	$(PY) -m venv .venv && .venv/bin/pip install -U pip ruff
	npm install
	@echo "Setup completato: ruff (.venv) + prettier (node_modules)"

render: ## Interpola i limiti da limits.env nei file Grafana
	$(PY) tools/render.py

test-e2e: render ## Avvia lo stack airgapped e stampa gli URL delle dashboard da testare
	$(DC) up -d
	@echo "Ricarico Grafana per applicare il provisioning (alert da limits.env)..."
	$(DC) restart grafana
	@echo "Attendo che Grafana sia pronto..."
	@for i in $$(seq 1 30); do \
		if curl -sf http://localhost:$(GRAFANA_PORT)/api/health >/dev/null 2>&1; then break; fi; \
		sleep 2; \
	done
	@IP=$$(hostname -I 2>/dev/null | awk '{print $$1}'); [ -z "$$IP" ] && IP=localhost; \
	echo ""; \
	echo "Stack avviato. Dashboard da monitorare (accesso anonimo in sola lettura):"; \
	echo "  Gestore (kiosk): http://$$IP:$(GRAFANA_PORT)/d/sagra-gestore?kiosk"; \
	echo "  Primi (kiosk):   http://$$IP:$(GRAFANA_PORT)/d/sagra-primi?kiosk"; \
	echo "  Secondi (kiosk): http://$$IP:$(GRAFANA_PORT)/d/sagra-secondi?kiosk"; \
	echo "  Alert:   http://$$IP:$(GRAFANA_PORT)/alerting/list"; \
	echo ""; \
	echo "Log in tempo reale: make logs — Stop: make down"

fmt: ## Formatta e pulisce tutto (python, yaml, json, env)
	.venv/bin/ruff check --fix .
	.venv/bin/ruff format .
	node_modules/.bin/prettier --write .
	$(PY) tools/fmt_env.py $(ENV_FILES)

lint: ## Verifica la formattazione senza modificare (CI/pre-avvio)
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .
	node_modules/.bin/prettier --check .
	$(PY) tools/fmt_env.py --check $(ENV_FILES)
