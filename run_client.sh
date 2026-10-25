#!/usr/bin/env bash
# Pi 클라이언트 실행. 연결만 확인하려면: ./run_client.sh --check
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
exec "$PY" run_client.py "$@"
