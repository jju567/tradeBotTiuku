#!/usr/bin/env bash
# pulli.sh - Päivitä koodi gitistä ja uudelleenkäynnistä vain aktiiviset palvelut
set -e

git pull
sudo systemctl restart tradebot-tiuku-daemon
sudo systemctl restart tradebot-tiuku-ui
echo "✅ tradeBotTiuku päivitetty ja palvelut käynnistetty uudelleen."
