#!/bin/bash
# Ночной пуш бэкапа конфигов в GitHub (после collect_configs.sh).
# Сначала коммит, потом pull --rebase: pull с незакоммиченными правками
# всегда падает. Если rebase не прошёл (в репо пушили с ноутбука и есть
# конфликт) — откатываем его, чтобы репо не зависло посреди rebase
# (так было 26.08–30.10.2026), и шлём алерт в Telegram ботом портала.
cd /opt/smart-home-git || exit 1

alert() {
  echo "ALERT: $1"
  local cfg token chat thread
  cfg=$(sudo -u postgres psql -d portal -AtF $'\t' -c \
    "SELECT key, value FROM app.settings WHERE key IN ('telegram_bot_token','telegram_chat_id','telegram_thread_id')" 2>/dev/null)
  token=$(awk -F'\t' '$1=="telegram_bot_token"{print $2}' <<<"$cfg")
  chat=$(awk -F'\t' '$1=="telegram_chat_id"{print $2}' <<<"$cfg")
  thread=$(awk -F'\t' '$1=="telegram_thread_id"{print $2}' <<<"$cfg")
  [ -n "$token" ] && [ -n "$chat" ] || return
  curl -s -o /dev/null --max-time 20 "https://api.telegram.org/bot$token/sendMessage" \
    -d chat_id="$chat" ${thread:+-d message_thread_id="$thread"} \
    --data-urlencode text="⚠️ Core: бэкап конфигов в git — $1"
}

git add .
git diff --cached --quiet || git commit -q -m "backup: $(date +%d%m%Y_%H%M%S)"

if ! git pull --rebase origin master; then
  git rebase --abort 2>/dev/null
  alert "конфликт при pull --rebase, пуш пропущен. Разобрать руками в /opt/smart-home-git."
  exit 1
fi

if ! git push origin master; then
  alert "git push не прошёл, см. /var/log/smart-home-backup.log"
  exit 1
fi
