#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-${REPO_ROOT}/venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "${REPO_ROOT}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    echo "Error: ${PYTHON_BIN} is required but was not found in PATH." >&2
    exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
    echo "Error: npm is required but was not found in PATH." >&2
    exit 1
fi

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    if [[ -e "${VENV_DIR}" ]]; then
        echo "Error: ${VENV_DIR} exists but is not a Python virtual environment." >&2
        exit 1
    fi

    echo "Creating Python virtual environment in ${VENV_DIR}..."
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

VENV_PYTHON="${VENV_DIR}/bin/python"

echo "Installing backend dependencies..."
"${VENV_PYTHON}" -m pip install -r requirements.txt
"${VENV_PYTHON}" -m pip install -r requirements-dev.txt

echo "Installing pre-commit hooks..."
"${VENV_PYTHON}" -m pre_commit install

echo "Installing frontend dependencies..."
(
    cd frontend
    npm ci
)

echo
echo "QuickStart complete. Activate the environment with:"
echo "  source ${VENV_DIR}/bin/activate"
echo
echo "Run the development servers with:"
echo "  DEBUG=true uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000"
echo "  (cd frontend && npm run dev)"
