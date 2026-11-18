PY=python3
FILE?=fixtures/input.json
run:
	PYTHONPATH=src $(PY) -m patenttrack.cli $(FILE)
run-mixed:
	PYTHONPATH=src $(PY) -m patenttrack.cli fixtures/mixed.json
test:
	PYTHONPATH=src $(PY) -m pytest -q
clean:
	rm -rf .pytest_cache **/__pycache__ *.db
