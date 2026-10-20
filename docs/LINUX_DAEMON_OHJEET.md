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
sudo cp deploy/tradebot-tiuku.service /etc/systemd/system/

# Muokkaa tarvittaessa User ja hakemistopolut vastaamaan ympäristöäsi:
# sudo nano /etc/systemd/system/tradebot-tiuku.service

# Ota palvelu käyttöön ja käynnistä
sudo systemctl daemon-reload
sudo systemctl enable tradebot-tiuku
sudo systemctl start tradebot-tiuku
```

### Vaihtoehto B: Erilliset palvelut (Suositeltu tuotannossa)

Jos haluat ajaa daemonia ja UI-paneelia toisistaan riippumattomina:

```bash
# Kopioi molemmat palvelut
sudo cp deploy/tradebot-tiuku-daemon.service /etc/systemd/system/
sudo cp deploy/tradebot-tiuku-ui.service /etc/systemd/system/

sudo systemctl daemon-reload

# Käynnistä Paper Trader -daemon (koodiajastin: suorittaa päivittäisen markkinasynkronoinnin klo 23:05)
sudo systemctl enable tradebot-tiuku-daemon
sudo systemctl start tradebot-tiuku-daemon

# Käynnistä Streamlit Web UI (portti 8502)
sudo systemctl enable tradebot-tiuku-ui
sudo systemctl start tradebot-tiuku-ui
```

---

## 📊 6. Palvelun Tilan ja Logien Seuranta

### Palvelun tila:
```bash
sudo systemctl status tradebot-tiuku-daemon
# tai yhdistetty:
sudo systemctl status tradebot-tiuku
```

### Reaaliaikaiset logit (`journalctl`):
```bash
# Seuraa daemonin reaaliaikaista tulostetta
sudo journalctl -u tradebot-tiuku-daemon -f

# Seuraa yhdistettyä palvelua
sudo journalctl -u tradebot-tiuku -f -n 100
```

### Palvelun uudelleenkäynnistys / sammutus:
```bash
sudo systemctl restart tradebot-tiuku-daemon
sudo systemctl stop tradebot-tiuku-daemon
```

---

## 🌐 7. Streamlit Web UI:n Saavutettavuus & Palomuuri

Oletuksena tradeBotTiukun Streamlit kuuntelee porttia `8502` (jotta se ei mene päällekkäin muiden sovellusten tai Eetun kanssa).

```bash
# Salli portti UFW-palomuurissa (Ubuntu)
sudo ufw allow 8502/tcp
```

Avaa selain osoitteessa: `http://<palvelimen-ip>:8502`

*(Valinnainen tuotantosuositus: voit asettaa Nginx reverse proxyn ja HTTPS Let's Encrypt -sertifikaatin ohjaamaan liikenteen porttiin 8502).*

---

## 📱 8. Etäylläpito & Koodipäivitykset Puhelimesta (Git Pull & Daemon Restart)

tradeBotTiukun hallintapaneelissa (`dashboard.py`) on sisäänrakennettu etäylläpito-osio: **"🛠️ Palvelimen Etäylläpito (Git & Daemon)"**.

Tämän avulla voit tehdä koodimuutokset esimerkiksi puhelimesta Antigravity-istunnossa, puskea ne gittiin (`git push origin main`), ja yhdellä klikkauksella päivittää palvelimen koodin ja käynnistää taustadaemonin uudelleen ilman SSH-yhteyttä.

### Miten se toimii:
1. Avaa hallintapaneeli selaimessa (`http://<palvelimen-ip>:8502`).
2. Avaa vasemmasta sivupalkista valikko: **🛠️ Palvelimen Etäylläpito (Git & Daemon)**.
3. Näet nykyisen Git-haaran, viimeisimmän commitin ja daemonin tilan (PID, koodiajastin).
4. Klikkaa: **`⬇️ Git Pull & 🔄 Restart Daemon`**.
   - Järjestelmä ajaa turvallisen `git pull --autostash origin main` (ei ylikirjoita palvelimen paikallisia seurantalistoja).
   - Käynnistää taustadaemonin siististi uudelleen uudella koodilla.
   - Tyhjentää Streamlit-välimuistin, jolloin myös UI ottaa heti uudet Python-moduulit käyttöön.

### Valinnainen: Sudoers-oikeus ilman salasanaa (suorille systemctl-komennoille)
Jos haluat antaa Streamlit-prosessille suoran oikeuden ajaa `systemctl restart`, lisää Linux-palvelimella sudoers-sääntö:
```bash
sudo visudo -f /etc/sudoers.d/tradebot-tiuku
```
Lisää tiedostoon rivi (korvaa `ubuntu` palvelimen käyttäjätunnuksella):
```text
ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl restart tradebot-tiuku-daemon, /bin/systemctl restart tradebot-tiuku, /bin/systemctl restart tradebot-tiuku-ui
```
*Huom: Vaikka sudoers-riviä ei olisi lisätty, järjestelmä käyttää automaattista fallbackia: se sulkee vanhan `main_controller.py`-prosessin, jolloin systemd (`Restart=always`) nostaa sen automaattisesti 5 sekunnissa uudelleen pystyyn.*
