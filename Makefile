# nicanav — common tasks.
#
#   make help          list targets
#   make test          run the offline test suite
#   make up            start the stack on this box
#   make nightly       run the full data pipeline once
#
# Everything that touches the deployed stack goes through infra/docker-compose.yml
# and reads secrets from infra/.env, which is never committed.

COMPOSE := docker compose -f infra/docker-compose.yml --env-file infra/.env
PYTHON  := python3
DATA    ?= data

.DEFAULT_GOAL := help
.PHONY: help venv install test test-all lint format check up down restart logs ps \
        build nightly tiles circle-tiles valhalla pois index qa golden kpis circle migrate psql \
        backup check-speeds clean-tmp

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- development

install:  ## Install python dependencies for development
	$(PYTHON) -m pip install -r requirements/dev.txt

test:  ## Run the offline test suite (no network, no docker)
	$(PYTHON) -m pytest -q -m "not network and not docker"

test-all:  ## Run every test, including ones needing services
	$(PYTHON) -m pytest -q

lint:  ## Check formatting and lint
	ruff check .
	ruff format --check .

format:  ## Apply formatting and safe lint fixes
	ruff check --fix .
	ruff format .

check: lint test  ## Everything CI runs

# ------------------------------------------------------------------ the stack

up:  ## Start the stack
	$(COMPOSE) up -d

down:  ## Stop the stack (data volumes survive)
	$(COMPOSE) down

restart:  ## Restart the API only (config or code change)
	$(COMPOSE) up -d --build api

ps:  ## Show service status
	$(COMPOSE) ps

logs:  ## Follow logs (make logs SERVICE=valhalla)
	$(COMPOSE) logs -f $(SERVICE)

build:  ## Rebuild the api and pipeline images
	$(COMPOSE) build api pipeline

# -------------------------------------------------------------------- the data

RUN_PIPELINE := $(COMPOSE) --profile tools run --rm pipeline

nightly:  ## Full pipeline: OSM -> tiles + graph + POIs + index + QA
	./pipeline/nightly.sh

circle:  ## Regenerate the 48.3 km curation circle around MGA
	$(PYTHON) scripts/circle48.py --output $(DATA)/osm/circle48.geojson

tiles:  ## Rebuild base.pmtiles from the current extract
	./pipeline/build_tiles.sh

circle-tiles:  ## Rebuild the offline archive for the 48.3 km circle
	./pipeline/build_tiles.sh --circle-only

valhalla:  ## Rebuild the routing graph from the current extract
	./pipeline/build_valhalla.sh

pois:  ## Re-run POI ingest, conflation, load and export
	$(RUN_PIPELINE) $(PYTHON) -m pipeline.pois.fetch_osm_pois
	$(RUN_PIPELINE) $(PYTHON) -m pipeline.pois.conflate \
		--input /data/exports/src_osm.geojsonseq \
		--input /data/exports/src_overture.geojsonseq \
		--output /data/exports/pois_merged.geojsonseq \
		--queue /data/exports/review_queue.json
	$(RUN_PIPELINE) $(PYTHON) -m pipeline.pois.load_pois
	$(RUN_PIPELINE) $(PYTHON) -m pipeline.pois.export_geojson

index:  ## Rebuild the Meilisearch index
	$(RUN_PIPELINE) $(PYTHON) -m pipeline.search.build_index

qa: golden kpis  ## Run the whole QA suite against the live stack

golden:  ## Check the golden routes against the running router
	$(RUN_PIPELINE) $(PYTHON) -m pipeline.qa.golden_routes

kpis:  ## Compute nightly data-quality KPIs
	$(RUN_PIPELINE) $(PYTHON) -m pipeline.qa.kpis

# ---------------------------------------------------------------- housekeeping

migrate:  ## Apply db/migrations to a running database
	@for f in db/migrations/*.sql; do \
		echo "-- $$f"; \
		$(COMPOSE) exec -T postgis psql -v ON_ERROR_STOP=1 -U nicanav -d nicanav -f - < $$f || exit 1; \
	done

psql:  ## Open a psql shell
	$(COMPOSE) exec postgis psql -U nicanav -d nicanav

backup:  ## Dump the database to data/backups/
	./scripts/backup_db.sh

check-speeds:  ## Validate the Nicaragua speed table before a graph rebuild
	@$(PYTHON) -c "import json,sys; \
d=json.load(open('infra/valhalla/default_speeds.json')); \
assert isinstance(d,list) and d, 'must be a non-empty array of regions'; \
[[(lambda t: (t in r) or sys.exit('missing table: '+t))(t) for t in ('rural','suburban','urban')] for r in d]; \
[[(len(r[t]['way'])==8) or sys.exit('way[] must have 8 road classes in '+t) for t in ('rural','suburban','urban')] for r in d]; \
print('default_speeds.json OK')"

clean-tmp:  ## Remove half-written build artifacts from a killed run
	find $(DATA) -name '*.tmp' -print -delete
