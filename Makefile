.PHONY: help install test demo web brief check clean dispensary stack
PY := ./.venv/bin/python

help:
	@echo "make install   create the venv and install everything"
	@echo "make test      run the full suite (no network, no credentials)"
	@echo "make demo      the three-act CLI demo"
	@echo "make web       serve the console on :8000"
	@echo "make stack     console + the mock internal hospital system"
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

# A stand-in for a customer's own internal system, so the custom-adapter path is
# demonstrable against real HTTP rather than a fixture.
dispensary:
	./.venv/bin/uvicorn services.dispensary.app:app --port 8900

# Both at once. Walnut auto-registers the dispensary when it is reachable; when it
# is not, the coverage table reports it as "not searched" rather than hiding it.
stack:
	@./.venv/bin/uvicorn services.dispensary.app:app --port 8900 & \
	sleep 2 && ./.venv/bin/uvicorn walnut.web.app:app --reload --port 8000

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
