install:
	python -m venv .venv
	.venv/bin/pip install -r requirements.txt

dev:
	.venv/bin/uvicorn app.main:app --reload --port 8000

test:
	.venv/bin/python scripts/smoke_test.py

key:
	.venv/bin/python scripts/create_key.py

.PHONY: install dev test key
