#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "创建虚拟环境 .venv …"
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi

if ! .venv/bin/python -c "import fastapi" >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r requirements.txt
fi

export READER_OPEN_BROWSER=1
exec .venv/bin/python main.py
