PYTHON ?= python3

.PHONY: init seed test

init:
	$(PYTHON) -m tc.db init

seed:
	$(PYTHON) -m tc.fixtures seed

test:
	$(PYTHON) -m pytest -q
