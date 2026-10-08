#!/bin/sh
# Daily database backup, keeps the last 14. Install with: crontab -e, then add
#   30 3 * * * /home/ubuntu/discord-bot/deploy/backup.sh
cd "$(dirname "$0")/.." || exit 1
mkdir -p data/backups
python3 -c "import sqlite3; s=sqlite3.connect('data/bot.db'); d=sqlite3.connect('data/backups/bot-$(date +%F).db'); s.backup(d)"
ls -1t data/backups/bot-*.db | tail -n +15 | xargs -r rm --
