#!/usr/bin/env bash
# Creates .env and .gitignore from the visible copies. Safe to re-run (never overwrites .env).
set -e
cd "$(dirname "$0")"
[ -f .gitignore ] || cp gitignore.txt .gitignore
[ -f .env.example ] || cp env.example.txt .env.example
[ -f .env ] || cp .env.example .env
chmod 600 .env
echo "Created/verified: .gitignore .env.example .env (chmod 600)."
echo "Now edit it:  nano .env      (list hidden files with: ls -la)"
