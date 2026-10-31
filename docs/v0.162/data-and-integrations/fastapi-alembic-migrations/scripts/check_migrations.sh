#!/usr/bin/env bash
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 MOZGII, Inc. (https://mozgii.com/)
# Check Alembic migrations before a merge.
#
# Usage: scripts/check_migrations.sh [base-ref] [versions-dir]
#   base-ref      git ref to compare against (default: origin/main)
#   versions-dir  folder with revision files (default: migrations/versions)
#
# Needs a scratch database in the URL your env.py reads (for example APP_DATABASE_URL), never a shared one:
# step 3 drops every table by downgrading to base.
set -euo pipefail

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  sed -n '4,11p' "$0" | cut -c3-
  exit 0
fi

BASE="${1:-origin/main}"
VERSIONS="${2:-migrations/versions}"

echo "1. Applied migrations are never edited or deleted"
changed=$(git diff --name-only --diff-filter=MDR "$BASE"...HEAD -- "$VERSIONS" || true)
if [ -n "$changed" ]; then
  echo "FAIL: these existing migrations were modified, renamed or deleted:" >&2
  echo "$changed" >&2
  exit 1
fi

echo "2. Exactly one head"
heads=$(uv run alembic heads | grep -c "(head)" || true)
if [ "$heads" -ne 1 ]; then
  echo "FAIL: $heads heads; run: uv run alembic merge heads -m \"merge heads\"" >&2
  exit 1
fi

echo "3. Upgrade, downgrade to base, upgrade again"
uv run alembic upgrade head
uv run alembic downgrade base
uv run alembic upgrade head

echo "4. Models and migrations agree"
uv run alembic check

echo "OK: migrations pass"
