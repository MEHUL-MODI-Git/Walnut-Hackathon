.PHONY: help install test demo web brief check clean
PY := ./.venv/bin/python

help:
	@echo "make install   create the venv and install everything"
	@echo "make test      run the full suite (no network, no credentials)"
	@echo "make demo      the three-act CLI demo"
	@echo "make web       serve the console on :8000"
	@echo "make brief     generate BRIEF.md from a live run"
	@echo "make check     what to run before recording the demo"

install:
	uv venv --python 3.12 .venv
	uv pip install --python $(PY) -e ".[dev,web]"

test:
	$(PY) -m pytest -q

demo:
	$(PY) demo.py

web:
	./.venv/bin/uvicorn walnut.web.app:app --reload --port 8000

brief:
	$(PY) -m walnut.brief > BRIEF.md && echo "wrote BRIEF.md"

# Everything that must be green before the demo is recorded.
check: test
	@$(PY) -m walnut.brief --no-tests > /dev/null && echo "brief: all checks PASS" \
		|| (echo "BRIEF HAS A FAILING CHECK — do not record"; exit 1)
	@$(PY) demo.py > /dev/null && echo "demo: runs clean"
	@git status --porcelain | grep -q . && echo "warning: working tree is dirty" || echo "git: clean"

clean:
	rm -rf .pytest_cache **/__pycache__ runs/
