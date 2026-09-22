# 🐧 tradeBotTiuku — Linux Daemon & Palvelinasennusohje

Tämä ohje opastaa **tradeBotTiuku** -järjestelmän (Paper Trader -taustadaemonin ja Streamlit Web Dashboardin) asentamiseen ja ajamiseen Linux-palvelimella (Ubuntu / Debian / AlmaLinux / RHEL) **systemd**-taustapalveluna.

---

## 📋 1. Esivaatimukset

Varmista, että palvelimella on asennettuna Python (3.10 tai uudempi), git ja virtuaaliympäristötyökalut:

```bash
# Ubuntu / Debian
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git curl

# RHEL / AlmaLinux / Fedora
sudo dnf install -y python3 python3-pip git curl
```

---

## 📂 2. Koodin kloonaus ja Virtuaaliympäristö

Suositeltava asennuspolku on `/opt/tradeBotTiuku` tai oman käyttäjäsi kotihakemisto (`~/tradeBotTiuku`).

```bash
# Kloonaa repositorio
cd /opt
sudo git clone <GIT_REPO_URL> tradeBotTiuku
sudo chown -R $USER:$USER /opt/tradeBotTiuku
cd /opt/tradeBotTiuku

# Luo virtuaaliympäristö ja asenna riippuvuudet
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

---

## 🔑 3. Ympäristömuuttujat (`.env`)

Luo projektin juureen `.env`-tiedosto ja aseta API-avaimet:

```bash
cat << 'EOF' > /opt/tradeBotTiuku/.env
OPENROUTER_API_KEY=oma_openrouter_avaimesi_tähän
# Valinnainen: OPENAI_API_KEY=...
EOF

# Suojaa tiedoston oikeudet
chmod 600 /opt/tradeBotTiuku/.env
```

---

## 🧪 4. Manuaalinen Testiajo

Ennen taustapalvelun käynnistämistä varmista, että ympäristö ja kirjastot toimivat:

```bash
# Aja yksi Paper Trader -tarkistuskierros
python3 main_controller.py --run-once

# Aja testisarja varmistukseksi
pytest tests/test_portfolio_manager.py
```

---

## ⚙️ 5. Systemd -Taustapalvelun Asennus

Järjestelmässä on valmiit `systemd`-yksikkötiedostot kansiossa `deploy/`.

### Vaihtoehto A: Yhdistetty palvelu (UI + Paper Trader samassa)

Tämä käynnistää sekä taustadaemonin että Streamlit-käyttöliittymän:

```bash
# Kopioi palvelutiedosto systemd-hakemistoon
sudo cp deploy/tradebot.service /etc/systemd/system/

# Muokkaa tarvittaessa User ja hakemistopolut vastaamaan ympäristöäsi:
# sudo nano /etc/systemd/system/tradebot.service

# Ota palvelu käyttöön ja käynnistä
sudo systemctl daemon-reload
sudo systemctl enable tradebot
sudo systemctl start tradebot
```

### Vaihtoehto B: Erilliset palvelut (Suositeltu tuotannossa)

Jos haluat ajaa daemonia ja UI-paneelia toisistaan riippumattomina:

```bash
# Kopioi molemmat palvelut
sudo cp deploy/tradebot-daemon.service /etc/systemd/system/
sudo cp deploy/tradebot-ui.service /etc/systemd/system/

sudo systemctl daemon-reload

# Käynnistä Paper Trader -daemon (tarkistaa 4h välein)
sudo systemctl enable tradebot-daemon
sudo systemctl start tradebot-daemon

# Käynnistä Streamlit Web UI (portti 8501)
sudo systemctl enable tradebot-ui
sudo systemctl start tradebot-ui
```

---

## 📊 6. Palvelun Tilan ja Logien Seuranta

### Palvelun tila:
```bash
sudo systemctl status tradebot-daemon
# tai
sudo systemctl status tradebot
```

### Reaaliaikaiset logit (`journalctl`):
```bash
# Seuraa daemonin reaaliaikaista tulostetta
sudo journalctl -u tradebot-daemon -f

# Seuraa yhdistettyä palvelua
sudo journalctl -u tradebot -f -n 100
```

### Palvelun uudelleenkäynnistys / sammutus:
```bash
sudo systemctl restart tradebot-daemon
sudo systemctl stop tradebot-daemon
```

---

## 🌐 7. Streamlit Web UI:n Saavutettavuus & Palomuuri

Oletuksena Streamlit kuuntelee porttia `8501`.

```bash
# Salli portti UFW-palomuurissa (Ubuntu)
sudo ufw allow 8501/tcp
```

Avaa selain osoitteessa: `http://<palvelimen-ip>:8501`

*(Valinnainen tuotantosuositus: voit asettaa Nginx reverse proxyn ja HTTPS Let's Encrypt -sertifikaatin ohjaamaan liikenteen porttiin 8501).*
