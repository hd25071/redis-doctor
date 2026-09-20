# redis-doctor — thin wrappers around rdctl.py so Linux/macOS/CI and Windows
# behave identically. On Windows run the same commands as:
#   python rdctl.py <target>
PY ?= python
RD  = $(PY) rdctl.py

.PHONY: help install lint test demo eval eval-full report api scenarios clean \
        cluster-check rbac-check backup

help:
	@$(RD) --help

install:
	$(PY) -m pip install -e ".[dev]"

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

test:
	$(PY) -m pytest

# One command: inject every scenario, run the agent against the sandbox
# cluster, print the trajectory of one fault. No Docker, no k3s, no API key.
demo:
	@$(RD) demo

# Full ablation A/B/C/D x 16 scenarios x 3 runs against the sandbox.
eval:
	@$(RD) eval --runs 3

# Same harness against a real k3s cluster (needs RD_BACKEND=real + kubeconfig).
eval-full:
	@$(RD) eval --runs 3 --backend real

report:
	@$(RD) report

api:
	@$(RD) serve --host 127.0.0.1 --port 8099

scenarios:
	@$(RD) scenarios list

cluster-check:
	@$(RD) cluster check

rbac-check:
	@$(RD) cluster rbac-check

backup:
	@$(RD) backup

clean:
	@$(RD) clean

