#!/bin/bash

# ===================================================
# Улучшенный watchdog для Payment Bot
# ===================================================
BOT_NAME="tg-bot"
LOG_FILE="/var/log/tg-bot-watchdog.log"
BOT_DIR="/work/barashek-bot"
MAX_RESTARTS=5
RESTART_WINDOW=300  # 5 минут
CHECK_INTERVAL=60   # 1 минута
HEALTH_CHECK_TIMEOUT=30

# Файл для отслеживания рестартов
RESTART_COUNTER="/tmp/tg-bot-restarts.counter"
LAST_RESTART_TIME="/tmp/tg-bot-last-restart.time"

# ===================================================
# Функции
# ===================================================

log_message() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"
}

check_service_status() {
    systemctl is-active --quiet "$BOT_NAME"
}

check_bot_responding() {
    # Проверяем, отвечает ли бот на ping
    cd "$BOT_DIR" || return 1
    
    # Проверяем наличие логов от бота за последние 2 минуты
    if journalctl -u "$BOT_NAME" --since "2 minutes ago" --no-pager -q | grep -q "PaymentBot\|🤖\|✅\|❌\⚠️"; then
        return 0
    fi
    
    return 1
}

check_error_patterns() {
    # Проверяем критические ошибки в логах
    local error_count=$(journalctl -u "$BOT_NAME" --since "5 minutes ago" --no-pager -q | grep -c -i "critical\|fatal\|exception\|traceback\|ошибка\|критически")
    
    if [ "$error_count" -gt 10 ]; then
        log_message "Too many errors detected ($error_count), investigating..."
        journalctl -u "$BOT_NAME" --since "5 minutes ago" --no-pager -q | tail -20 >> "$LOG_FILE"
        return 1
    fi
    
    return 0
}

check_memory_usage() {
    # Проверяем использование памяти процессом бота
    local bot_pid=$(pgrep -f "python.*bot.py")
    if [ -n "$bot_pid" ]; then
        local memory_kb=$(ps -p "$bot_pid" -o rss= | tr -d ' ')
        local memory_mb=$((memory_kb / 1024))
        
        # Если процесс использует более 1GB памяти
        if [ "$memory_mb" -gt 1024 ]; then
            log_message "High memory usage detected: ${memory_mb}MB"
            return 1
        fi
    fi
    
    return 0
}

check_task_count() {
    # Проверяем количество активных задач (если бот отвечает на /system)
    cd "$BOT_DIR" || return 1
    
    # Пытаемся получить статистику задач через бот
    # Это требует наличия админского ID и работающего бота
    return 0  # Пропускаем эту проверку для простоты
}

count_recent_restarts() {
    local current_time=$(date +%s)
    local last_restart=$(cat "$LAST_RESTART_TIME" 2>/dev/null || echo "0")
    local restart_count=$(cat "$RESTART_COUNTER" 2>/dev/null || echo "0")
    
    # Если прошло больше окна времени, сбрасываем счетчик
    if [ $((current_time - last_restart)) -gt "$RESTART_WINDOW" ]; then
        echo "0" > "$RESTART_COUNTER"
        echo "0" > "$LAST_RESTART_TIME"
        restart_count=0
    fi
    
    echo "$restart_count"
}

increment_restart_counter() {
    local current_count=$(count_recent_restarts)
    local new_count=$((current_count + 1))
    local current_time=$(date +%s)
    
    echo "$new_count" > "$RESTART_COUNTER"
    echo "$current_time" > "$LAST_RESTART_TIME"
    
    echo "$new_count"
}

graceful_restart() {
    log_message "Attempting graceful restart..."
    
    # Сначала отправляем SIGTERM для graceful shutdown
    local bot_pid=$(pgrep -f "python.*bot.py")
    if [ -n "$bot_pid" ]; then
        kill -TERM "$bot_pid"
        sleep 10
        
        # Если процесс все еще работает, принудительно убиваем
        if kill -0 "$bot_pid" 2>/dev/null; then
            kill -KILL "$bot_pid"
            log_message "Force killed bot process"
        fi
    fi
    
    # Перезапускаем сервис
    systemctl restart "$BOT_NAME"
    sleep 5
    
    if check_service_status; then
        log_message "Bot service restarted successfully"
        return 0
    else
        log_message "Failed to restart bot service"
        return 1
    fi
}

# ===================================================
# Основная логика
# ===================================================

main() {
    # Проверяем, запущен ли сервис
    if ! check_service_status; then
        log_message "Bot service is not running, starting..."
        systemctl start "$BOT_NAME"
        exit 0
    fi
    
    # Считаем количество недавних рестартов
    local recent_restarts=$(count_recent_restarts)
    
    # Если слишком много рестартов, прекращаем на некоторое время
    if [ "$recent_restarts" -ge "$MAX_RESTARTS" ]; then
        log_message "Too many restarts ($recent_restarts), waiting for manual intervention"
        # Можно добавить отправку уведомления администратору
        exit 1
    fi
    
    # Проверяем различные проблемы
    local problems_found=0
    
    # 1. Проверяем, отвечает ли бот
    if ! check_bot_responding; then
        log_message "Bot is not responding to health checks"
        problems_found=$((problems_found + 1))
    fi
    
    # 2. Проверяем наличие критических ошибок
    if ! check_error_patterns; then
        log_message "Critical errors detected in logs"
        problems_found=$((problems_found + 1))
    fi
    
    # 3. Проверяем использование памяти
    if ! check_memory_usage; then
        log_message "Memory usage issues detected"
        problems_found=$((problems_found + 1))
    fi
    
    # 4. Проверяем логи от самого бота за последние 2 минуты
    local bot_logs=$(journalctl -u "$BOT_NAME" --since "2 minutes ago" --no-pager -q | grep -E "PaymentBot|🤖|✅|❌|⚠️|INFO|ERROR|WARNING")
    if [ -z "$bot_logs" ]; then
        log_message "No bot logs detected in last 2 minutes"
        problems_found=$((problems_found + 1))
    fi
    
    # Если найдены проблемы, перезапускаем
    if [ "$problems_found" -gt 0 ]; then
        local new_restart_count=$(increment_restart_counter)
        log_message "Detected $problems_found problem(s), restarting bot (restart #$new_restart_count)"
        
        if graceful_restart; then
            log_message "Bot restart completed successfully"
        else
            log_message "Bot restart failed, needs manual intervention"
        fi
    else
        # Если все хорошо, сбрасываем счетчик рестартов
        echo "0" > "$RESTART_COUNTER"
        echo "0" > "$LAST_RESTART_TIME"
        log_message "Bot health check passed"
    fi
}

# Запускаем основную логику
main
