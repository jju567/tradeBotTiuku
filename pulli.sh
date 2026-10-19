#!/usr/bin/env bash
# pulli.sh - Päivitä koodi gitistä ja uudelleenkäynnistä vain aktiiviset palvelut
set -e

git checkout -- data/clean_microcap_universe.csv 2>/dev/null || true
git pull
sudo systemctl restart tradebot-tiuku-daemon
sudo systemctl restart tradebot-tiuku-ui
echo "✅ tradeBotTiuku päivitetty ja palvelut käynnistetty uudelleen."
