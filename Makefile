VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
UVICORN := $(VENV)/bin/uvicorn
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
