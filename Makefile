VENV := .venv
PYTHON := $(VENV)/bin/python
# PIP_USER=0: some environments (e.g. Replit) point PIP_CONFIG_FILE at a config
# that forces --user installs, which a venv rejects outright.
PIP := PIP_USER=0 $(VENV)/bin/python -m pip
# Invoke uvicorn as a module rather than via $(VENV)/bin/uvicorn: pip writes that
# console script with a "#!/usr/bin/env python3" shebang when the venv's python is
# a symlink (Nix/Replit), so it resolves to the *system* python and can't see the
# venv's packages.
UVICORN := $(PYTHON) -m uvicorn
HOST := 0.0.0.0
PORT := 8000
PID_FILE := .server.pid
LOG_FILE := logs/server.log

.PHONY: up down restart logs status venv install clean

venv:
	test -d $(VENV) || python3 -m venv $(VENV)

install: venv
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -r requirements.txt
	$(PIP) install -q --no-deps mt5linux==0.1.9

up: install
	mkdir -p logs data
	@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		echo "Already running (pid $$(cat $(PID_FILE))). Use 'make restart' or 'make down'."; \
	else \
		nohup $(UVICORN) app.main:app --host $(HOST) --port $(PORT) > $(LOG_FILE) 2>&1 & \
		echo $$! > $(PID_FILE); \
		sleep 1; \
		echo "Started (pid $$(cat $(PID_FILE))). Dashboard: http://localhost:$(PORT)"; \
	fi

down:
	@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		kill $$(cat $(PID_FILE)); \
		rm -f $(PID_FILE); \
		echo "Stopped."; \
	else \
		echo "Not running."; \
		rm -f $(PID_FILE); \
	fi

restart: down up

logs:
	tail -f $(LOG_FILE)

status:
	@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		echo "Running (pid $$(cat $(PID_FILE))) — http://localhost:$(PORT)"; \
	else \
		echo "Not running."; \
	fi

clean: down
	rm -rf $(VENV) __pycache__ app/__pycache__ */__pycache__
