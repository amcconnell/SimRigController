SHAKER_DIR := apps/shaker
FRONTEND_DIR := $(SHAKER_DIR)/frontend
ANSIBLE_DIR := ansible

.PHONY: dev test lint format sync _unhide \
        frontend-install frontend-dev frontend-build \
        ansible-deps ansible-ping ansible-syntax ansible-check ansible-deploy

# macOS Sequoia tags files written by uv with com.apple.provenance, which sets
# UF_HIDDEN. Python 3.13's site.py skips hidden .pth files, breaking the
# editable install. Sync first, then clear the flag, then run with --no-sync —
# `uv run`'s implicit sync would re-hide the .pth AFTER the unhide otherwise.
_unhide:
	@-chflags nohidden $(SHAKER_DIR)/.venv/lib/python*/site-packages/*.pth 2>/dev/null || true

sync:
	cd $(SHAKER_DIR) && uv sync
	@$(MAKE) --no-print-directory _unhide

dev: sync
	cd $(SHAKER_DIR) && uv run --no-sync python -m shaker

test: sync
	cd $(SHAKER_DIR) && uv run --no-sync pytest

lint: sync
	cd $(SHAKER_DIR) && uv run --no-sync ruff check .

format:
	cd $(SHAKER_DIR) && uv run ruff format .

# --- Frontend (Vite + React) ----------------------------------------------

frontend-install:
	cd $(FRONTEND_DIR) && npm install

# Live UI development against the running Pi (proxies /api/* to simrig-pi.local).
frontend-dev:
	cd $(FRONTEND_DIR) && npm run dev

# Production build — emits hashed bundles into src/shaker/web/static/.
# Run this before `make ansible-deploy` after any UI change.
frontend-build:
	cd $(FRONTEND_DIR) && npm run build

# --- Ansible deployment ----------------------------------------------------

ansible-deps:
	cd $(ANSIBLE_DIR) && ansible-galaxy collection install -r requirements.yml

ansible-ping:
	cd $(ANSIBLE_DIR) && ansible all -m ping

ansible-syntax:
	cd $(ANSIBLE_DIR) && ansible-playbook site.yml --syntax-check

ansible-check:
	cd $(ANSIBLE_DIR) && ansible-playbook site.yml --check --diff --ask-become-pass

ansible-deploy: frontend-build
	cd $(ANSIBLE_DIR) && ansible-playbook site.yml --ask-become-pass
