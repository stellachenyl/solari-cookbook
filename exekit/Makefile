# ExecKit developer targets. POSIX paths are the default; on Windows (Git Bash
# with cygwin make) set VENV_BIN=Scripts, e.g.:  make dev VENV_BIN=Scripts
VENV_BIN ?= bin

install:
	python3 -m venv .venv
	.venv/$(VENV_BIN)/pip install -r requirements.txt

dev:
	.venv/$(VENV_BIN)/python -m uvicorn app.main:app --reload --port 8000

test:
	.venv/$(VENV_BIN)/python scripts/smoke_test.py

key:
	.venv/$(VENV_BIN)/python scripts/create_key.py

credits:
	.venv/$(VENV_BIN)/python scripts/add_credits.py

.PHONY: install dev test key credits
