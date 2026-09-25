# 🐱 tradeBotTiuku — Avointen Lähteiden Salkunneuvonantaja & Advisory AI Agent

<img src="tiuku.svg" alt="tradeBotTiuku Icon" width="90" style="border-radius: 12px;">

**tradeBotTiuku** on monipuolinen, Python-pohjainen ja paikallisesti toimiva salkunseuranta-, mikroyhtiöseulonta- ja uudelleentasapainotuksen neuvonantaja-agentti. Tiuku toimii täysin itsenäisenä **avointen lähteiden markkina-älyagenttina (Advisory Agent)**. Järjestelmä yhdistää teknisen analyysin, fundamentti-NLP-tilinpäätöslukijan, monikielisen reaaliaikaisen uutistarkistuksen (Suomi, Ruotsi, USA) sekä modernin interaktiivisen Streamlit-hallintapaneelin ihmisen hyväksyttäväksi (*Human-in-the-loop*).

---

## 🌟 Tärkeimmät Ominaisuudet

### 1. 🖥️ Interaktiivinen Streamlit-Hallintapaneeli & Yhdistetty Käynnistys
- **Yhdistetty Pikakäynnistys (UI + Paper Trader)**:
  - `start_all.bat` tai `.\start_all.ps1` tai `python start_all.py`
  - Käynnistää sekä Streamlit-hallintapaneelin että reaaliaikaisen Paper Trader -daemonin yhdellä komennolla ja hallitsee molempien prosessien siistiä sammutusta (`Ctrl+C`).
  - *Parametrit*: `--interval-hours <h>` (oletus: 4.0h), `--run-once` (yksi ajo), `--ui-only` (vain UI), `--daemon-only` (vain daemon), `--port <port>` (oletus: 8502).
- **Pelkkä Käyttöliittymä**: `streamlit run dashboard.py --server.port 8502`
- **Pelkkä Paper Trader Daemon**: `python main_controller.py --loop --interval-hours 4`
- 🐧 **Linux-palvelinasennus & systemd-daemon**: Täydellinen asennusopas ja valmiit palvelupohjat löytyvät dokumentista: [docs/LINUX_DAEMON_OHJEET.md](docs/LINUX_DAEMON_OHJEET.md).
- **Saumaton Taustapäivitys Ilman Harmaantumista (`st.fragment` & Anti-Dimming CSS)**: Hyödyntää moduulitason staattisia `@st.fragment`-kääreitä ja räätälöityä CSS-koodia, joka estää Streamlitin himmenemisen / latauspeitteen automaattisen päivityksen aikana.
- **Selkeät Välilehdet**:
  - 📋 **Seulonnan Tulokset**: Suodatettava ja haettava taulukko analysoiduista raporteista, Suomen aikavyöhykkeen aikaleimoilla (`DD.MM.YYYY HH:MM:SS`), profiileilla ja tuomioilla (🔥 *STRONG BUY*, 👀 *WATCH_TURNAROUND*, 🟡 *HOLD*, ❌ *REJECT*).
  - 👀 **Turnaround-Seurantalista (`watchlist_turnarounds.csv`)**: Yhtiöt, joissa on havaittu reaaliaikaisia positiivisia käännekatalyyttejä (*suurtilaukset, yrityskaupat, velkajärjestelyt, uusi johto*). Seurantalistaa voi hallinnoida suoraan näkymästä (`🗑️ Poista listalta`).
  - 🔎 **Live Ticker Scanner**: Yksittäisen osakkeen ad-hoc-pikatarkistus (fundamenttiluvut, uutistarkistus ja Dual-Step -tuomio). Havaitut turnaround-kandidaatit voidaan tallentaa suoraan yhdellä klikkauksella Turnaround-seurantalistalle (`➕ Tallenna Turnaround Watchlistiin`).
  - 📊 **ETF-Seuranta**: Buy the Dip -indikaattorit ja kuukausisäästön allokaatio.
  - 💼 **Salkun Yhteenveto & Paper Trading Portfolio (`data/open_positions.csv`)**: Reaaliaikaiset spot-hinnat, automaattiset FX-valuuttakurssimuunnokset (EUR), avoin PnL (€ ja %), salkun kokonaisarvo ja käteissaldo, tilastollinen KPI-yhteenveto. **Reaaliaikainen markkinapohjainen salkkurekonstruktio (`compute_live_portfolio_history`)**: salkun tuottokäyrää ei enää lueta pelkistä staattisista JSON-tallenteista, vaan se rekonstruoidaan suoraan salkun avointen osakkeiden todellisista historiallisista päätöskursseista (`yfinance`, 1h ja 1d -palkit, valuuttamuunnokset EUR). Näin graafi heijastaa aina 100 % tarkasti osakkeiden todellista kurssikehitystä ilman teennäisiä tasoheilahteluita. Valittavissa aikavälit (24h, 7d, 30d, 3kk, kaikki historia), resoluutiot (tunti 1h, kaikki mittauspisteet, päivä 1d, viikko 1vk, kuukausi 1kk) ja mittarit (kokonaisarvo €, tuotto € / %, käteinen vs. osakkeet). **Kaikissa graafeissa esitetään reaaliaikaiset aikaleimat**: graafin päivityshetki (`🕒 Päivitetty`), mittauspisteen tai markkinanoteerauksen uusin aikaleima (`📅 Uusin data`) sekä moottorimerkintä (`📡 Moottori`).

  - 🪙 **Token-Laskuri & Kustannusseuranta**: Reaaliaikainen syöte-/tuotostokenien laskenta, pyyntömäärät, arvioitu dollarikulu ja jäljellä oleva analyysikapasiteetti.
  - ⚙️ **Massa-ajon Hallinta**: Suora käynnistyspainike raporteille valinnaisella reaaliaikaisella web-uutistarkistuksella.

---

### 2. 🔍 Dual-Lens Mikroyhtiöseulonta & Massa-analyysi (`batch_processor.py`)
> *Yksityiskohtainen kuvaus poimintalogiikasta löytyy dokumentista: [docs/OSAKEPOIMINTALOGIIKKA.md](docs/OSAKEPOIMINTALOGIIKKA.md). Täydellinen testausmetodologia ja tulokset on koottu raporttiin: [docs/BACKTEST_RAPORTTI.md](docs/BACKTEST_RAPORTTI.md).*

- **Mikroyhtiöuniversumin Eristys (`scripts/clean_universe_builder.py`) & Dynaaminen Universumipäivitys (`screener/dynamic_universe_updater.py`)**:
  - Hakee reaaliaikaiset FX-kurssit ja suodattaa osakeuniversumin tiukasti alle $300M USD markkina-arvoon (`< $300M USD`) estäen suuryhtiökontaminaation.
  - **Tiukat likviditeetti- ja senttiosakesuodattimet**: Hylkää osakkeet, joiden hinta on alle 0.10 (paikallisessa valuutassa) tai joiden 20 päivän keskimääräinen päivävaihto (20d ADV) on alle $50,000 USD, karsien epälikvidit tilauskirjat ja sub-penny -ansat puhtaaseen laatulikvidin mikroyhtiön universumiin (US, FI, SE).
  - **Automaattinen Viikkosynkronointi (Nordnet & yfinance)**: Skannaa 7 päivän välein (tai `--sync-universe` -lipulla / Dashboardin pikapainikkeella) Nordnetin rajapinnasta uudet listautujat (esim. uudet First North / Spotlight -yhtiöt). Jos uuden tulokkaan 20d ADV ylittää 50 000 € / $50k ja hinta $\ge$ 0,10, se lisätään atomisesti `clean_microcap_universe.csv`:hen ja otetaan automaattisesti mukaan kaupankäyntiin. Tila tallennetaan tiedostoon `data/universe_sync_status.json`.
  - Päivitetty **Profile A (Quality Growth)**: vaatii liikevaihdon kasvun (> 20 %) ohella vahvaa myyntikatetta (> 40 %) ja eloonjäämistarkastuksen (positiivinen OCF tai kassariittävyys > 18 kk).

- **Yhdistetty Testipatteristo (`run_all_tests.py`)**:
  - Ajaa yhdellä komennolla kaikki 5 testausmoduulia (pytest, Lost in the Middle, Ground Truth, 2.5x ATR Trailing Stop-Loss ja Markkinavaikutus/DSR).

- **Kvantitatiivinen Fundamenttibäkkäri (`scripts/fundamental_backtester.py`)**:
  - Testaa historiallisesta tilinpäätösdatasta (yfinance) deterministiset kovat säännöt täysin erillään LLM:stä.
  - Arvioi kvartaalikohtaisesti Profile A (Kasvu: liikevaihto YoY > 20 %, myyntikate > 40 %, kassariittävyys) ja Profile B (Arvo: nettovelaton tase, Anti-Shrinking -sääntö).
  - Laskee automaattisesti 3 kk (~63 kaupankäyntipäivää) ja 6 kk (~126 kaupankäyntipäivää) toteutuneet tuotot, vertailuindeksin (esim. `^RUT`) tuoton, alfan sekä maksimilaskut (Max Drawdown).

- **LLM Ground Truth -testipatteri (`scripts/llm_truth_tester.py`)**:
  - Validoi laadullisen päättelyn ja determinististen turvaporttien toimivuuden tunnetuilla testiskenaarioilla (Ruotsin kontrollbalansräkning, ATM-diluutiokriisi, johdon ostot ja käännekatalyytti, rutiinikalenterit, hyperkasvuyhtiöt).
  - Vertaa LLM-tuomiota ja pedagogista perustelua odotettuun maaperätotuuteen (Ground Truth) ja tulostaa tarkan tarkkuusraportin.

- **Konteksti-ikkunan & 'Lost in the Middle' -Testipatteri (`scripts/context_window_tester.py`)**:
  - Testaa analyysiputken tarkkaavaisuutta ja kestävyyttä massiivisen 50-sivuisen (~7 000 – 30 000 tokenia) tilinpäätöksen keskelle (50 % kohta) upotettua kriittistä myrkkykapselia vastaan (*Ruotsin kontrollbalansräkning tai \$25M ATM-diluutio*).
  - Varmistaa, että petollisen hyvät kovat fundamenttiluvut eivät sokaise järjestelmää ohittamaan tekstin kriittisiä riskivaroituksia.

- **Institutionaalinen Harhaton Bäkkäri (`scripts/institutional_backtester.py`)**:
  - Eliminoi eloonjäämis- (survivorship bias) ja ennakkonäkemisharhan (look-ahead bias) lukemalla fundamentit puhtaasta **Point-in-Time** -tietokannasta (`data/clean_microcap_pit_fundamentals.csv`, 672 kvartaaliraporttia aikaväliltä 02/2025–09/2026) historiallisen kurssi- ja valuuttatiedon kera.
  - Simuloi 240 toteutunutta mikroyhtiökauppaa 69 eri yhtiölle ($N=240, N_{\text{eff}} \approx 69$) vähentäen **0.50 %** kulusuojan/slippagen.
  - Tuottaa kattavat institutionaaliset riskimittarit: keskituotto vs. mediaani (vinouden paljastamiseksi), Sharpe-luku (3 % Rf), volatiliteetti (Std Dev), keskimääräinen Max Drawdown sekä segmentoidut alfat suhteessa Russell 2000 (`^RUT`) -indeksiin.

- **2.5x ATR Trailing Stop-Loss -Simulaattori (`scripts/stop_loss_simulator.py`)**:

  - Simuloi mekaanisen 14 päivän 2.5x ATR dynaamisen liukuvan tappionpysäytyksen toteutusta fundamenttisignaaleille (`data/institutional_backtest_results.csv`).
  - Vertailee 6 kk Buy & Hold -tuottoa ja hallittua riskiä, rajoittaen suurimman tappion (-27.83 %) ja vapauttaen pääoman uusiin ideoihin (ka. pitoaika 26.9 pv).

- **Vaihe 0: Deterministinen Tase- ja Kassavirtalaskenta (`screener/financial_metrics_engine.py`)**:
  - Hakee taseen ja kassavirrat yfinancesta ja laskee kovat talousluvut (`net_cash`, `operating_cash_flow_ttm`, `cash_runway_months`, `revenue_growth_yoy_pct`) puhtaalla Pythonilla ennen LLM-kutsua.
  - Sisältää **Data Freshness Guard** -tarkistuksen (enintään 120 päivää vanha tilinpäätös, `DATA_STALENESS_WARNING`), joka suojaa vanhentuneen datan pohjalta syntyviltä ostoilta.
  - Syöttää luvut tekoälylle lohkossa `[HARD FINANCIAL FACTS - DO NOT RECALCULATE]`, jolloin LLM ei hallusinoi laskutoimituksia ja keskittyy laadulliseen analyysiin.
- **Vaihe 1: Fundamentti-NLP -analyysi (`screener/nlp_analyzer.py`)**:
  - **Profiili A (Hyper-Growth Tech / Micro-Cap)**: Skaalautuvat 10x-kandidaatit (korkea bruttokate, orgaaninen liikevaihdon kasvu, laajeneva TAM).
  - **Profiili B (Deep Value / Turnaround)**: Matala arvostus (P/B, EV/S), vahva tase, kassan riittävyys ja orgaaninen kannattavuuskäänne.
  - **Kovat Turvaportit**: Hylkää automaattisesti yhtiöt, joilla on jatkuva laimennusriski (*ATM-annit, toksiset vaihtovelkakirjat*), Ruotsin lain miinat (*kontrollbalansräkning, rekonstruktion*), epämääräiset liiketoimintakäännökset tai kassan loppuminen alle 6–12 kuukaudessa.
- **Vaihe 2: Markkinakohtainen Reaaliaikainen Uutis- ja Tiedotetarkistus (`screener/web_verifier.py`)**:
  - **Automaattinen Markkinareititys (`detect_market`)**: Tunnistaa Suomen (`.HE`), Ruotsin (`.ST`) ja Yhdysvaltain (`US`) osakkeet.
  - **Lokalisoitu Google News RSS & Tiedotekanavat**: Hakee tuoreet uutiset markkina-alueen omilla parametreilla (`fi-FI`, `sv-SE`, `en-US`) ja kohdentaa hakulausekkeet pörssitiedotteisiin (Cision, MFN, GlobeNewswire).
  - **Monikielinen Sanasto & Taivutusmuodot**: Tunnistaa pohjoismaiset riskit (*osakeanti, suunnattu anti, yrityssaneeraus, kontrollbalansräkning, företrädesemission, sammanläggning av aktier*) ja käännekatalyytit (*suurtilaus, merkittävä sopimus, yrityskauppa, kannattavuuskäänne, stororder, förvärv, positiv vinstvarning*).
  - **Monikielinen LLM-tuomari**: Tulkitsee monikieliset uutisotsikot suoraan kontekstissa ilman käännösvirheitä.

---

### 3. 📊 ETF-Seurantalista & Dippi-Indikaattorit (`etf_watchlist.json`)
- Luokiteltu seurantalista (Maailma-indeksit, USA, Eurooppa/Pohjoismaat, Osinko/Value, Teemat & Sektorit, Korkorahastot).
- Laskee indeksisijoittamiseen räätälöidyt **Buy the Dip** -signaalit (RSI ≤ 40, hinta alle 50d keskiarvon) kuukausisäästön kohdentamiseksi.

---

### 4. 💼 Salkunhallinta, Kulusuojaus & Markkinavahti
- **📥 Nordnet CSV-Salkuntuonti**: Tuo omistukset, kappalemäärät ja keskihinnat suoraan Nordnetin CSV-/sivutaulukkotiedostosta (`--import-csv`).
- **🛡️ Nordnet Palkkiotasot & Markkinakohtainen Kulusuojaus**:
  - **OMX Helsinki (`.HE`)**: Kotimaan Taso 3 minimipalkkio 7,00 € / 0,15 %.
  - **Ulkomaiset pörssit (Saksa `.DE`, Lontoo `.L`, USA)**: Ulkomaankaupan minimipalkkio 15,00 € / 0,15 %.
  - **Nordnet-rahastot (`NN_NORGE`, `NN_SVERIGE`)**: 0,00 € välityspalkkio.
  - Suodattaa automaattisesti pois pikkukaupat, joiden välityspalkkiot ylittäisivät 2,5 % kauppasummasta.
- **🎯 Mukautettavat Sijoitusstrategiaprofiilit**:
  - `SWING_TRADING` (Aktivoi +10 % TP, -8 % SL, nopeat osittaiset voittojen kotiutukset).
  - `LONG_TERM` (Pitkä osta ja pidä, +50 % TP, ei myyntejä tilapäisestä overbought-tilasta).
  - `HYBRID` (Oletus: ETF:t `LONG_TERM`, osakkeet `SWING_TRADING`).
- **🛡️ Kolmitasoinen Fundamentti- ja Uutispohjainen Exit-Strategia (`portfolio_manager.py`)**:
  - **Taso 1: Päivittäinen Katastrofisuoja (-50 %)**: Laukaisee välittömän `CATASTROPHIC_STOP`-myynnin, jos hinta puolittuu ostotasosta (rajaa yksittäisen position tappion max 2,5 %:iin salkun kokonaispääomasta).
  - **Taso 2: Tapahtumapohjainen LLM-uutistutka & Dual Confirmation**: Erottelee välittömät fataalit insolvenssitapahtumat (`FATAL_RED_FLAG_PATTERNS`: konkurssi, kontrollbalansräkning, saneeraus, petostutkinnat) ja hallinnolliset/dilutiiviset varoitukset (`WARN_PATTERNS`: reverse split pörssilistauksen turvaamiseksi, osakeanti, kassapuskuri < 4 qtr). Fataaleista poistutaan välittömästi seuraavassa avauksessa (`LLM_NEWS_REJECT`), kun taas varoituksille (`WARN`) vaaditaan **Dual Confirmation** (hintatason heikkeneminen -5 % tiedotepäivästä tai -10 % ostotasosta) ennen myyntiä (`LLM_NEWS_WARN_CONFIRMED`). Tämä eliminoi väärät hälytykset (0 % virhemyyntejä $N=10$-otoksessa) ja antaa voittajien (`WRAP`, `CODX`, `WATT`) juosta.
  - **Taso 3: Kvartaalikohtainen Fundamenttiheikkeneminen**: Laukaisee `FUNDAMENTAL_DETERIORATION`-myynnin virallisen osavuosikatsauksen julkistuspäivänä, jos liikevaihto supistuu YoY (< 0 %) tai kassariittävyys putoaa alle 12 kuukauden samalla kun operatiivinen kassavirta on negatiivinen.
  - Korvaa kapeat ja kohina-alttiit mekaaniset trailing stopit, jolloin multibaggereiden nousupotentiaali säilyy katkeamattomana.
- **⚡ Nolla-Token Markkinavahti & PnL-Hystereesi (`MarketMonitor`)**: Valvoo salkun hintoja 15 min välein 0 tokenin kulutuksella. Älykäs PnL-hystereesi estää toistuvat hälytykset samasta staattisesta tuottotasosta.
- **🔇 Sähköpostihälytysten Älykäs Esto & 24h Cooldown (`sent_alerts`)**: Tallentaa lähetetyt hälytykset SQLite-tietokantaan (`data/processed_news.db`). Estää saman yhtiön (`FARON.HE` / `FARON`) toistuvat spämmisähköpostit 24 tunnin jäähdytysajalla.
- **⏳ Anti-Whipsaw & Uudelleenoston 30 pv Cooldown (`REENTRY_COOLDOWN_DAYS = 30`)**: Estää myydyn osakkeen välittömän takaisinoston (churn/wash-trade) 30 päivän kuluessa myynnistä, säästäen kaupankäyntikulut. Lisäksi seulonnassa suoritetaan **Pre-Entry Exit Validation**, joka hylkää kandidaatin heti, jos sen fundamentit laukaisisivat exit-säännön (esim. YoY supistuva liikevaihto).


---

## 🚀 Asennus ja Käyttöönotto

### 1. Riippuvuuksien asennus

```bash
# Luo ja aktivoi virtuaaliympäristö
python -m venv .venv
.venv\Scripts\Activate.ps1   # Linux/macOS: source .venv/bin/activate

# Asenna riippuvuudet
pip install -r requirements.txt
```

### 2. Ympäristömuuttujat (`.env`)
Kopioi `.env.example` nimelle `.env`:
```bash
cp .env.example .env
```
Aseta API-avaimet:
```env
OPENROUTER_API_KEY="your-openrouter-api-key"
OPENAI_API_KEY="your-openai-api-key" # Valinnainen fallback

# Salkkuasetukset
NORDNET_FEE_TIER=3
INVESTMENT_STRATEGY="HYBRID"
```

---

## 🖥️ Käyttö & Komennot

### 1. Web-Hallintapaneeli (Streamlit)
```bash
streamlit run dashboard.py --server.port 8502
```
Avaa selaimessa osoitteen `http://localhost:8502`.

### 2. Massaraporttien Seulonta (Batch Processor)
```bash
# Aja seulonta kaikille historiallisille raporteille (PDF/TXT) reaaliaikaisella web-uutistarkistuksella:
python batch_processor.py --web-check

# Testaa nopeasti vain 10 ensimmäistä raporttia:
python batch_processor.py --limit 10 --web-check
```

### 3. Salkun Päivittäminen & Nordnet CSV-Sisäänluku
```bash
# Tuo salkku Nordnetin vientitiedostosta:
python main.py --import-csv "data/Osaketaulukko_salkku.csv"

# Näytä nykyinen salkun tila:
python main.py --show-portfolio

# Päivitä käteissaldo (EUR):
python main.py --set-cash 1500.00
```

### 4. Viikkoanalyysi & Markkinavahti
```bash
# Aja viikkoanalyysi ja luo HTML-raportti kerran:
python main.py --run-once

# Aja nolla-token markkinavahdin tarkistus kerran:
python -m scheduler.market_monitor --once

# Käynnistä jatkuva salkun tausta-ajastin:
python main.py --schedule
```

### 5. 🤖 Autonominen Seulontadaemon (`screener/main_controller.py`)
Jatkuvasti taustalla pyörivä ja markkina-aikatietoinen (EET / `Europe/Helsinki`) monisäikeinen daemon:
- **Pörssin aukioloaika (ma–pe 07:30–23:30 EET)**: Tarkistusväli 300 s (5 min).
- **Suljettu pörssi / viikonloput**: Virransäästötila 1800 s (30 min).
- **Monisäikeiset taustatyöntekijät**: Syötteiden nouto (`run_feed_check`), turnaround-seurantalistan katalyyttitutkinta (`scan_watchlist_catalysts`) ja paperipositioiden exit-seuranta (`update_paper_positions`).
- **Automaattihuollot**: Päivittäinen tilansiivous klo 06:00 EET ja EOD-huolto klo 19:00 EET.

```bash
# Käynnistä itsenäinen taustadaemon:
python -m screener.main_controller --loop

# Aja seulontaputki kerran läpi reaaliaikaisille syötteille:
python -m screener.main_controller --run-once

# Aja älykäs massaskannaus (hakee vain uusimman 10-Q/10-K/osavuosikatsauksen SEC/Nordic):
python -m screener.main_controller --mass-scan
```

### 6. 🚀 Multi-Portfolio Live Walk-Forward Testing Engine (`main_controller.py`)
Juuritason päämoottori reaaliaikaiseen 11 rinnakkaisen paperisalkun walk-forward -testaukseen ja tilastojen keräämiseen:
- **Konfigurointi YAML-tiedostolla (`portfolios_config.yaml`)**:
  - Määrittelee 11 toisistaan erotettua salkkua, joilla jokaisella on 10,000 € virtuaalipääoma ja omat riskiparametrit:
    1. `P1_Base`: Profile B, dead money 180d, min ADV 50k, 10 slottia (1,000 €/osto), alueet FI/SE/US.
    2. `P2_Fast_Cycle`: Profile B, lyhyt dead money 90d (nopea pääoman kierrätys).
    3. `P3_Diamond_Hands`: Profile B, pitkä dead money 365d (kestää väliaikaiset pohjamudat).
    4. `P4_Institutional`: Profile B, korkea likviditeettivaatimus (min ADV 250,000 €).
    5. `P5_Nordic_Only`: Profile B, sijoittaa ainoastaan Pohjoismaihin (FI, SE).
    6. `P6_US_Only`: Profile B, sijoittaa ainoastaan Yhdysvaltoihin (US).
    7. `P7_Deep_Value_Extreme`: Profile B, lisäsuodatin `price_to_cash < 0.5` (markkina-arvo alle puolet nettokassasta).
    8. `P8_Quality_Growth`: Profile A, sijoittaa kasvu- ja kannattavuussignaaleihin (kasvu > 20 %, kate > 40 %).
    9. `P9_High_Conviction`: Profile B, vain 5 slottia (2,000 €/osto) suurella vakaumuksella.
    10. `P10_Micro_Sniper`: Profile B, 20 pientä slottia (500 €/osto, min ADV 150k).
    11. `P11_Meta_Consensus`: Meta_Consensus, 5 slottia (2,000 €/osto). Ei skannaa raakamarkkinaa, vaan treidaa P1–P10 -perussalkkujen reaaliaikaista konsensusta: vaatii sisääntuloon Conviction Scoren $\ge 8$ (⭐⭐⭐⭐+) sekä tagit `Institutional` (+1) JA (`Deep Value` (+2) TAI `Quality Growth` (+1)). Myy automaattisesti, jos vakaumuspistemäärä putoaa alle 6 pisteen (`CONVICTION_DROP`) tai suojastopit laukeavat.
- **Yhteinen Markkinadatan Nouto (Single-Pass Market Engine)**:
  - Hakee reaaliaikaiset kurssit, 20d ADV:t, uutiset ja deterministiset fundamentit **tiukasti vain kerran syklissä** (säästää API-kutsuja ja estää yfinance-bannit).
- **Itsenäinen Tilanhallinta (`data/portfolios/`)**:
  - Jokainen salkku ylläpitää omaa tilaansa: `portfolio_<id>_state.json` (käteinen, aloitussaldo, valuutta, avoimet positiot).
  - Kauppahistoria kirjataan erilliseen lokiin: `portfolio_<id>_history.csv`.
  - Tuottokehityksen aikasarja tallennetaan tiedostoon: `portfolio_<id>_history.json`.
- **Sähköposti-integraatio (`email_notifier.py`)**:
  - Kevyt SMTP-hälytin (`.env`-tunnukset: `SMTP_SERVER`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `ALERT_EMAIL`).
  - **Ajon kauppayhteenveto (Consolidated Run Summary)**: Ei tuki sähköpostia kymmenillä yksittäisillä viesteillä, vaan lähettää kunkin suoritussyklin päätteeksi **yhden selkeän koontiviestin**, joka listaa kaikki toteutetut ostot, myynnit, varoitukset ja salkkujen päivitetyt saldot/tuotot.
  - Vikaturvallinen (fail-safe): jos SMTP-yhteys epäonnistuu, moottori jatkaa toimintaansa keskeytyksettä.
- **Pysyvä NLP-Päätösarkisto (`data/nlp_decisions_archive.csv`)**:
  - Jokainen arvioitu tiedote tai uutisotsikko tallennetaan pysyvään lokiin (`Timestamp, Ticker, Headline, LLM_Decision, Reasoning`).
  - Kerää pitkän aikavälin dataa LLM- ja sääntöpohjaisista tuomioista (REJECT / WARN / HOLD) mallin ja suodatinten jatkoanalyysiä varten.
- **Streamlit-Käyttöliittymä**:
  - Sivupalkin valikosta valittavissa mikä tahansa 11 rinnakkaissalkusta.
  - Avaa ja visualisoi reaaliaikaiset avoimet positiot, KPI-arvot, tuottokäyrän ja toteutuneet kaupat valitulle salkulle reaaliaikaisine FX-muunnoksineen.
  - **📊 Salkkuvertailu (Kaikki)**: Erillinen välilehti, jossa kaikkien 11 salkun pääomakehitystä ja tuottoja voi vertailla rinnakkain interaktiivisella Plotly-viivakaaviolla (sisältää myös normalisoidun indeksinäkymän lähtötasolla 100 ja yhteenvedon).
  - **⭐ Top Picks & Conviction (`dashboard.py` / `quant_analytics.py`)**: Uusi monisalkkukonsensuksen ja turvallisuusluokituksen välilehti:
    - **Painotettu konsensuspistemäärä (0–14 p)**: +1 p jokaisesta perussalkusta (max 10 p) + likviditeettipreemio `P4_Institutional` (+1 p, ADV > 250k) + Deep Value -preemio `P7_Deep_Value_Extreme` (+2 p, Price/Cash < 0.5) + Quality-preemio `P8_Quality_Growth` (+1 p, korkea kasvu ja kannattavuus).
    - **Visuaalinen tähtiluokitus**: >10 p = ⭐⭐⭐⭐⭐, 8–10 p = ⭐⭐⭐⭐, 5–7 p = ⭐⭐⭐, <5 p = ⭐⭐.
    - **Interaktiivinen taulukko**: `st.column_config.ProgressColumn` -edistymispalkit (0–14), tähtiluokitussuodatus ja hakukenttä.
  - **🏛️ Institutional Analytics (`quant_analytics.py`)**: Uusi dedikoitu pääomarahastotason kvantitatiivisen analyysin välilehti Streamlitissä:
    - **Markkinaregiimiseuranta (Market Regime Tagging)**: Seuraa aitoa mikroyhtiöiden vertailuindeksiä (iShares Micro-Cap ETF / `IWC`, Russell Microcap Index $< \$300M$) ja luokittelee markkinan tilaan (*🟢 BULL / LOW VOL, 🟡 NEUTRAL / RANGE-BOUND, 🔴 HIGH VOLATILITY / BEARISH*) 20 päivän toteutuneen volatiliteetin ja 50 päivän liukuvan keskiarvon (SMA50) perusteella mikroyhtiöille kalibroiduin kynnysarvoin (Low Vol $< 20\%$, Stress $> 30\%$, SMA50 puskuri $-5\%$). Tukee myös sisäisen 106 osakkeen puhtaan universumin toteutunutta volatiliteettia (`clean_microcap_universe.csv`).
    - **Riskikorjatut tuotot**: 30 päivän rullaava Sharpe-luku, 30 päivän rullaava Sortino-luku (downside deviation), Max Drawdown ja toipumisaika (Time-to-Recovery päivinä).
    - **Monitestauskorjaus & Datan Riittävyysportti (Data Sufficiency Guard)**: Deflated Sharpe Ratio (DSR, Bailey & López de Prado) ja Bonferroni-korjaus 11 rinnakkaissalkun yli. Sisältää sisäänrakennetun riittävyysportin ($T \ge 20$ päivää), joka estää pienen otoskoon varianssiräjähdyksen (kuten pienen $T$:n poikkileikkaushajonnasta syntyneen *Null E[max] = 15,74* -laskenta-artefaktin) ja näyttää standardin normalisoidun satunnaishuipun ($SR^* \approx 1.57$) vasta riittävällä datamäärällä.
    - **Tuottoattribuutio & Vinous**: Posiokeskittyminen (% kokonaistuotosta top-1 ja top-2 kaupoista), mediaanipitoaika päivinä sekä mediaanituotto vs. keskituotto.
    - **11 Salkun Tuottokorrelaatiomatriisi**: Interaktiivinen Plotly-lämpökartta (heatmap) päivä- ja viikkotuottojen riippuvuuksista todellisen hajautushyödyn arvioimiseksi.
    - **Viikkoaggregointi (Weekly Smoothing)**: Poistaa päivittäistä mikroheilahtelua ja markkinakohinaa.
    - **Botin Operatiivinen Terveys (`data/operational_metrics.json`)**: Reaaliaikaiset KPI-kortit LLM fallback -asteesta ja tuoreussuojasta (`is_fresh`). Alkuvaiheen pienillä ajoilla tilana on `🟡 INITIALIZING (Pieni otos — odottaa syklejä)` ennen todistettua stressirasitusta.


```bash
# Aja yksi walk-forward-sykli kaikille 10 salkulle:
python main_controller.py --run-once

# Aja vain tietty salkku (esim. P5_Nordic_Only):
python main_controller.py --run-once --portfolio P5_Nordic_Only

# Käynnistä jatkuva silmukka (oletus 24h välein):
python main_controller.py --loop --interval-hours 24

# Nollaa kaikki 10 salkkua aloitustilaan (10,000 €):
python main_controller.py --reset-portfolios

# Single-Stock Time Machine: Testaa Layer 2 -uutistutka historiallisilla tiedotteilla:
python single_stock_news_backtest.py --ticker SEZI.ST --buy-date 2026-02-16
```

### 7. Yksikkötestit
```bash
pytest
```
Koko testipatteristo (264 testiä) varmistaa salkunhallinnan, riskilaskennan, raporttien NLP-analyysin, live-forward-daemonin, exit-tasot ja monikielisen web-verifioinnin toimivuuden.

---

## 📂 Repositorion Rakenne

```
tradeBotTiuku/
├── main_controller.py       # Live Forward-Testing Daemon (Paperisalkku & Tri-Layer Exit)
├── single_stock_news_backtest.py # Single-Stock Time Machine: Layer 2 LLM News Radar -bäkkäri
├── dashboard.py             # Streamlit-pohjainen reaaliaikainen web-hallintapaneeli
├── batch_processor.py       # Massaraporttien eräajoprosessori (Phase 1 + Phase 2)
├── run_all_tests.py         # Yhdistetty päätestipatteri (5 moduulia)
├── universe_builder.py      # Mikroyhtiö- ja likviditeettieristys (< $300M, ADV >= $50k)
├── institutional_backtester.py # Harhaton PIT-bäkkäri (2.5x ATR, 0.5% kulusuoja)
├── stop_loss_simulator.py   # 2.5x ATR liukuva stop-loss -simulaattori
├── historical_pdf_crawler.py# Monilähde-crawler pörssiraporteille (Nordic / US)
├── main.py                  # CLI-pääkäynnistystiedosto ja salkunhallinta
├── config.py                # Järjestelmäasetukset & Nordnet-palkkiotasot
├── screener/
│   ├── nlp_analyzer.py      # Dual-Lens NLP -tilinpäätösanalyysi (Growth & Value)
│   ├── web_verifier.py      # Monikielinen reaaliaikainen tiedote- & uutistarkistus (FI, SE, US)
│   ├── main_controller.py   # Seulontaputken orkestroija
│   ├── financial_metrics_engine.py # Deterministinen fundamenttilaskenta
│   ├── backtest_engine.py   # Toro-Bouchaud, Half-Kelly & DSR -laskenta
│   └── crawler.py           # Tiedotteiden ja SEC/Nordic-julkaisujen noutaja
├── core/
│   ├── ai_advisor.py        # 1-10 Pisteytys & Tekninen AI-analyysi
│   ├── rebalancer.py        # Uudelleentasapainotus & Conviction-logiikka
│   └── risk_manager.py      # Stop Loss & Kulusuojaus
├── clients/
│   ├── market_data_client.py# yfinance-markkinadatayhteys & indikaattorit
│   └── nordnet_client.py    # Salkkulaskenta ja omistusseuranta
├── data/
│   ├── clean_microcap_universe.csv # Suodatettu likvidi mikroyhtiölista (106 kpl)
│   ├── clean_microcap_pit_fundamentals.csv # Harhaton kvartaali-PIT-aineisto (672 kpl)
│   ├── institutional_backtest_results.csv # Bäkkärin toteutuneet kaupat (240 kpl)
│   ├── paper_account.json   # Paperitestitilin tila ($10,000 käteinen & pääoma)
│   ├── open_positions.csv   # Paperitestin aktiiviset positiot
│   ├── trade_history.csv    # Paperitestin suljetut kaupat ja toteutunut PnL
│   ├── historical_reports/  # Tilinpäätös- ja osavuosikatsausraportit (PDF/TXT)
│   ├── batch_results.csv    # Seulonnan konsolidoidut tulokset
│   ├── watchlist_turnarounds.csv # Tunnistetut käänneyhtiöt
│   ├── batch_status.json    # Massa-ajon reaaliaikainen edistymistila
│   └── etf_watchlist.json   # ETF-seurantalistan konfiguraatio
└── tests/                   # Kattava pytest-testikokoelma (261 testiä)
```

---

## 🔭 Jatkoseuranta & Tuleva Kalibrointi (Roadmap & Monitoring)

Seuraavat kaksi kvantitatiivista kohtaa on kirjattu seurattavaksi ajan mittaan, kun paperisalkut keräävät dataa useamman kuukauden ajalta:

1. **Divergenssihälytyksen kynnysten (20 % / 40 %) empiirinen toimivuus**:
   - Tarkkaillaan, laukeaako hälytys järkevällä taajuudella: sen tulee varoittaa todellisesta mikroyhtiöstressistä silloin, kun ETF-benchmark (IWC) näyttää vielä matalaa volatiliteettia, muttei laukoa vääriä hälytyksiä jatkuvasti normaalissa markkinatilassa.
   - Hienosäädetään 20 % / 40 % -kynnyssuhdetta tarvittaessa havaitun korrelaation ja varianssisuhteen perusteella.
2. **Mikroyhtiöiden persentiilikynnysten päivitys (Q1 / Q3)**:
   - Nykytilan nyrkkisääntökynnykset (28 % matala / 45–50 % korkea) korvataan täsmällisillä 106-yhtiön historiallisen volatiliteettijakauman $Q_1$- ja $Q_3$-persentiileillä, kunhan 3–6 kuukauden kattava empiirinen aikasarja on kertynyt.

---

## 🔒 Tietosuoja & Disclaimer

- **Tietosuoja & API-avaimet**: Salkkutiedot ja API-avaimet säilytetään omalla paikallisella laitteellasi.
- **Tekoälykutsut**: Tekoälyanalyysit hyödyntävät ulkoisia kielimallirajapintoja (OpenRouter / OpenAI / Gemini) turvallisesti HTTPS-rajapintojen yli.
- **Human-in-the-loop**: *Ei automaattista toimeksiantojen suoritusta* — kaikki kaupat suoritetaan aina ihmisen toimesta Nordnetissä.
- **Ympäristöt & Turvallisuus**:
  - `C:\`: Testi- ja kehitysympäristö.
  - `Z:\`: Tuotantoympäristö. **Z:-asemaa ei saa koskaan suoraan ylikirjoittaa automaatiolla.**

