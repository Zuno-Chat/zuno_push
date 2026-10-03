VENV := .venv
PY := $(VENV)/bin/python
SRC := src unit_tests contract_tests

# Creates .venv only when missing, so a venv built on another interpreter
# (the server's Python minor) is reinstalled into, never overwritten.
$(VENV)/.ok: pyproject.toml
	test -x $(PY) || python3 -m venv $(VENV)
	$(PY) -m pip install -q -e '.[dev]'
	touch $@

venv: $(VENV)/.ok

test: venv
	$(PY) -m twisted.trial unit_tests

# Synapse's own test harness at the pinned tag; the first run clones it and
# builds .contract/venv-<version>. Set SYNAPSE_POSTGRES=1 and the
# SYNAPSE_POSTGRES_* variables for the Postgres run.
contract:
	scripts/contract-tests.sh

check: venv
	$(VENV)/bin/ruff format --check $(SRC)
	$(VENV)/bin/ruff check $(SRC)
	$(PY) -m twisted.trial unit_tests

fmt: venv
	$(VENV)/bin/ruff format $(SRC)
	$(VENV)/bin/ruff check --fix $(SRC)

.PHONY: venv test contract check fmt
