#!/usr/bin/env bash
set -euo pipefail

if [[ -x ".venv/bin/python" ]]; then
    quality_python=".venv/bin/python"
else
    quality_python="${PYTHON:-python3}"
fi

"$quality_python" -m ruff check app mock_llm scripts tests
"$quality_python" -m mypy
"$quality_python" -m bandit -q -c pyproject.toml -r app mock_llm scripts
"$quality_python" -m pip_audit --local
"$quality_python" -m pytest
