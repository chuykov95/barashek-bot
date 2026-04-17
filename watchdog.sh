#!/bin/bash
# Проверяем, были ли логи за последние 2 минуты
LAST_LOG=$(journalctl -u tg-bot --since "2 minutes ago" --no-pager -q)

if [ -z "$LAST_LOG" ]; then
    echo "$(date) - Bot is stuck, restarting..." >> /var/log/tg-bot-watchdog.log
    systemctl restart tg-bot
fi
