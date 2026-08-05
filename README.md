# 🐱 tradeBotTiuku — Avointen Lähteiden Salkunneuvonantaja & Advisory Agent

![tradeBotTiuku Icon](tiuku.svg)

**tradeBotTiuku** on Python-pohjainen, paikallisesti toimiva salkunseuranta- ja uudelleentasapainotuksen neuvonantaja-agentti. Koska suoraa pankki-API-yhteyttä (kuten Nordnet API) ei ole saatavilla uusille asiakkaille, Tiuku toimii täysin itsenäisenä **avointen lähteiden markkina-älyagenttina (Advisory Agent)**. Järjestelmä laskee salkun arvon, tekee teknisen ja AI-pohjaisen analyysin sekä tuottaa valmiit toimenpide-ehdotukset (Checklist) ja visuaalisen HTML-dashboardin ihmisen hyväksyttäväksi (*Human-in-the-loop*).

---

## 🌟 Tärkeimmät Ominaisuudet

- **📁 Tiedostopohjainen Salkunseuranta**: Lukee omistukset, määrät ja hankintahinnat paikallisesta `tiuku_portfolio.json` -tiedostosta.
- **📈 Avointen Lähteiden Markkinadata (`yfinance`)**: Hakee reaaliaikaiset kurssit ja tekniset indikaattorit (RSI, Bollinger %B, SMA/EMA) ilmaiseksi ilman maksullisia API-avaimia.
- **🔒 HODL / Lottolappu -Suojapuskuri**: Voit lukita yksittäisiä osakkeita (esim. `FARON.HE`), jotta automaattinen myynti ei koskaan koske niihin.
- **🛡️ Nordnet Palkkiotasot & Kulusuojaus**: Integroitu Nordnetin palkkiotasot (Taso 3 min 7.00 € / 0.15 %). Suodattaa pois pikkukaupat, joiden välityspalkkiokulut ylittäisivät 2.5 % kauppasummasta.
- **🎯 Conviction Alignment**: Osto-ohjelmaan pääsevät vain ja ainoastaan osakkeet, joiden tekninen AI-arvio antaa ostosuosituksen (`BUY` / `STRONG_BUY`).
- **🌐 Visuaalinen HTML Dashboard**: Generoi jokaisen ajokerran yhteydessä selkeän, modernin `tiuku_dashboard.html` -näkymän `tiuku.svg` -ikonilla.

---

## 🚀 Asennus ja Käyttö

### 1. Riippuvuuksien asennus
```bash
python -m venv .venv
source .venv/bin/scripts/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Ympäristömuuttujat (`.env`)
Kopioi mallitiedosto `.env.example` nimelle `.env`:
```bash
cp .env.example .env
```
Määritä tarvittaessa OpenAI API-avain (jos käytät GPT-4o -analyysiä):
```env
OPENAI_API_KEY="your-api-key-here"
NORDNET_FEE_TIER=3
```

### 3. Aja Analyysisykli
Aja viikkoanalyysi ja dashboard-sukupolvi kerran:
```bash
python main.py --run-once
```

Käynnistä ajastin (esim. joka maanantai 08:00 AM):
```bash
python main.py --schedule
```

---

## 📂 Repositorion Rakenne

```
tradeBotTiuku/
├── tiuku.svg                # Tiuku-kissan tavaramerkki/app-ikoni
├── tiuku_dashboard.html     # Visuaalinen selain-dashboard
├── tiuku_portfolio.json     # Paikallinen salkun tila
├── main.py                  # Pääkäynnistystiedosto
├── config.py                # Järjestelmäasetukset & Nordnet-palkkiotasot
├── clients/
│   ├── market_data_client.py# yfinance-markkinadatayhteys & indikaattorit
│   └── nordnet_client.py    # Advisory-tilan salkkulaskenta
├── core/
│   ├── ai_advisor.py        # 1-10 Pisteytys & Tekninen AI-analyysi
│   ├── rebalancer.py        # Uudelleentasapainotus & Conviction-logiikka
│   └── risk_manager.py      # Stop Loss & Kulusuojaus
└── reporting/
    └── weekly_reporter.py   # Markdown & HTML Dashboard -generaattori
```

---

## 🔒 Tietosuoja & Disclaimer

- Järjestelmä toimii 100 % paikallisesti omalla laitteellasi.
- *Ei automaattista toimeksiantojen suoritusta* — kaikki kaupat suoritetaan ihmisen toimesta (Human-in-the-loop).
