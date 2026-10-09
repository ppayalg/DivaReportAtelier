PY ?= python3
export PYTHONPATH := src:tests

.PHONY: install test validate frontend-check smoke demo serve docker check

install:            ## editable install (adds the `diva-atelier` command)
	$(PY) -m pip install -e .

test:               ## run the full test suite
	$(PY) -m unittest discover -s tests -v

validate:           ## validate every shipped configuration
	$(PY) -m diva_atelier validate configs/*.json

frontend-check:     ## syntax-check the dashboard JavaScript (needs node)
	$(PY) scripts/check_frontend.py

smoke:              ## start the server, generate a report over HTTP, stop
	bash scripts/smoke.sh

demo:               ## generate a bridal report to var/bridal.md using the offline mock model
	@mkdir -p var
	$(PY) -m diva_atelier generate configs/bridal.json --vars examples/bridal_vars.json --author priya --out var/bridal.md
	@echo "open var/bridal.md"

serve:              ## dashboard + API on http://127.0.0.1:8000
	$(PY) -m diva_atelier serve

docker:             ## build the container image
	docker build -t diva-atelier-ai .

check: validate test frontend-check smoke   ## everything CI runs
