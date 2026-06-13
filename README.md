# AI Trading — Alpaca

Een persoonlijk handelssysteem op de Alpaca API met vier delen:

1. **Trailing stops** — beschermen je posities (native order óf een eigen ratchet-engine).
2. **Research-agents** — halen dagelijks nieuws op en scoren per bedrijf bullish/bearish.
3. **Swing-strategie** — combineert trend/momentum met sentiment tot koopsignalen, met streng risicobeheer.
4. **Dashboard** — toont je account, equity-curve, posities, sentiment, signalen en trades.

> ⚠️ **Geen beleggingsadvies.** Dit is software om zelf te leren en te oefenen.
> Handelen brengt risico mee; consistent "de markt verslaan" is aantoonbaar
> moeilijk. Begin op **paper** (oefengeld) en draai live pas als je het wekenlang
> hebt gevalideerd.

---

## Snel starten

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # vul je Alpaca-keys in (paper-keys = PK...)

.venv/bin/python cli.py status         # check je account
.venv/bin/python cli.py run            # dagelijkse research + signalen (advisory)
.venv/bin/python cli.py dashboard      # open het dashboard (http://localhost:8501)
```

`.env` en je Word-documenten staan in `.gitignore` en worden **nooit** gecommit.
🔐 Bewaar keys niet in een Word-doc/OneDrive; roteer ze in het Alpaca-dashboard als dat toch gebeurd is.

---

## De beleggingsstrategie (eerlijk)

Voor jouw profiel — **swing trading**, posities van dagen tot weken — doet het systeem dit:

- **Handel mét de trend.** Alleen kopen als de koers boven zijn voortschrijdende
  gemiddelden (SMA20 > SMA50) ligt én het momentum positief is. Tegen de trend in
  gaan is waar de meeste mensen geld verliezen.
- **Sentiment als bevestiging, niet als orakel.** Een positieve nieuwsstroom moet
  de trend *steunen*; het is een extra filter, geen koopknop op zich.
- **Risicobeheer is de echte edge.** Niet het signaal maar de discipline houdt je
  in leven: klein per positie (default max 5% van je equity), gespreid (max 8
  posities), en een **dagverlies-kill-switch** (default −3%) die alles stopzet.
- **Elke entry krijgt een trailing stop** (default 8%) zodat winst kan doorlopen
  maar een omkering je beschermt.

De eerlijke waarheid: dit verslaat niet gegarandeerd een simpele indexstrategie.
Het geeft je een gedisciplineerd, herhaalbaar proces — dat is wat telt.

---

## Onderdelen

### 1. Trailing stops

```bash
# Native (server-side, set & forget) — beschermt een bestaande positie
.venv/bin/python cli.py native AAPL --trail-percent 3

# Managed engine (blijft draaien, schuift een echte stop-order mee, ATR mogelijk)
.venv/bin/python cli.py engine AAPL --trail-percent 3 --poll 5
.venv/bin/python cli.py engine AAPL --atr-mult 3 --activation-percent 2
```

De engine plaatst altijd een echte stop-order op Alpaca als vangnet en schuift die
alleen omhoog (long) / omlaag (short) — nooit terug. Ctrl-C laat de stop staan.

### 2. Research-agents (nieuws → sentiment)

Bron is de **Alpaca News API** (gratis bij je account, Benzinga-nieuws). Sentiment
wordt gescoord door **Claude** als je een `ANTHROPIC_API_KEY` in `.env` zet, anders
door een gratis financieel woordenboek (lexicon).

> **Over Bloomberg/FT:** die zijn betaald en achter een login; hun voorwaarden
> verbieden scrapen en dat doen we niet. Wel kun je gratis nieuws-aggregators
> (Finnhub, NewsAPI, RSS) bijplaatsen in `research/news.py`, en met een echt
> Bloomberg/FT-abonnement hun officiële API koppelen. De watchlist staat in
> `watchlist.json` — pas die naar wens aan.

### 3. Dagelijkse run

```bash
.venv/bin/python cli.py run                 # advisory: alleen signalen, GEEN orders
.venv/bin/python cli.py run --execute       # plaats orders op het account van .env (paper)
```

Volgorde: nieuws ophalen → sentiment scoren → trend berekenen → signaal → (optioneel) traden.
Alles wordt opgeslagen in `data/trading.db` en verschijnt op het dashboard.

### 4. Dashboard

```bash
.venv/bin/python cli.py dashboard
```

Toont account + dagresultaat, equity-curve (3 mnd), open posities met P/L,
het laatste sentiment per symbool (🟢/🔴/⚪), recente signalen en trades.

---

## Auto-trading & veiligheid

Auto-traden staat standaard **uit** (`run` is advisory). Inschakelen:

| Wil je… | Commando |
|---|---|
| Alleen advies | `cli.py run` |
| Auto-traden op **paper** | `cli.py run --execute` |
| Auto-traden **live** (echt geld) | `cli.py run --live --execute --i-understand-live-risk` + env `ALLOW_LIVE_AUTOTRADE=true` |

Live vereist **alle** schakelaars tegelijk — zo kan het niet per ongeluk. Daarnaast:
elke entry krijgt een trailing stop, er gelden harde limieten (positiegrootte,
aantal posities), en de kill-switch stopt bij te veel dagverlies.

Limieten aanpassen:
```bash
.venv/bin/python cli.py run --execute --max-position-pct 3 --max-positions 5 \
    --max-daily-loss 2 --trail-percent 6 --min-confidence 0.5
```

---

## Elke dag automatisch draaien

macOS (cron) — bv. elke handelsdag om 16:00 (NL), ná de Amerikaanse opening:
```bash
crontab -e
# m h dom mon dow  command
0 16 * * 1-5  cd "/pad/naar/Trading" && .venv/bin/python daily_run.py >> run.log 2>&1
```
`daily_run.py` draait standaard in **advisory**-modus. Pas het aan of roep de CLI
met `--execute` aan als je orders wilt laten plaatsen.

---

## In de cloud deployen

> **Niet op Vercel.** Vercel draait alleen korte serverless-functies — geen
> Streamlit-dashboard en geen langlopende processen. Verwijder het Vercel-project;
> elk onderdeel hoort op een passende plek (hieronder).

### Agents elke dag in de cloud — GitHub Actions

`.github/workflows/daily.yml` draait elke handelsdag (`daily_run.py`) en commit
daarna `snapshot.json` terug, zodat het dashboard de verse data toont.

Zet in GitHub → **Settings → Secrets and variables → Actions** deze secrets:

| Secret | Waarde |
|---|---|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | je keys (PK… = paper, AK… = live) |
| `ALPACA_PAPER` | `true` (paper) of `false` (live) |
| `ANTHROPIC_API_KEY` | optioneel — schakelt Claude-sentiment in |
| `EXECUTE` | `true` om orders te plaatsen (laat leeg/`false` voor alleen advies) |
| `ALLOW_LIVE_AUTOTRADE` | `true` is **vereist** voor LIVE traden |

**Live auto-traden in de cloud** gebeurt alleen als álle drie kloppen: een live
`AK…`-key, `EXECUTE=true`, én `ALLOW_LIVE_AUTOTRADE=true`. Eén ervan vergeten →
geen live orders. **Begin op paper** (`ALPACA_PAPER=true`, eventueel `EXECUTE=true`)
en zet pas live als je het wekenlang vertrouwt.

Tijd aanpassen: de cron `0 14 * * 1-5` staat in UTC (≈ 10:00 New York zomertijd).
Handmatig starten kan via de **Actions**-tab → "Run workflow".

### Dashboard online — Streamlit Community Cloud

1. Ga naar [share.streamlit.io](https://share.streamlit.io) en log in met GitHub.
2. "New app" → repo `cjjdogterom/trading`, branch `main`, **main file** `dashboard/app.py`.
3. Bij **Advanced → Secrets** (TOML-formaat):
   ```toml
   ALPACA_API_KEY = "..."
   ALPACA_SECRET_KEY = "..."
   ALPACA_PAPER = "true"
   ```
   (zet hier dezelfde keys als je in het account wilt zien)
4. Deploy. Account/posities/equity komen live van Alpaca; sentiment/signalen/trades
   uit de `snapshot.json` die de GitHub Action dagelijks bijwerkt.

---

## Tests

```bash
.venv/bin/python tests/test_engine.py     # trailing-stop logica (nep-broker, geen netwerk)
```

---

## Projectstructuur

```
cli.py                  command-line interface (status/buy/native/engine/run/dashboard)
daily_run.py            dagelijkse pipeline (nieuws -> sentiment -> signalen -> traden)
trailing_stop/          Alpaca-wrapper + trailing-stop (native + managed engine)
research/               watchlist, Alpaca-nieuws, sentiment (Claude + lexicon-fallback)
strategy/               signalen (trend+sentiment), risicobeheer, executor (live-grendel)
storage/                SQLite-opslag voor nieuws/sentiment/signalen/trades
dashboard/app.py        Streamlit-dashboard
watchlist.json          welke symbolen gevolgd worden (bewerkbaar)
```

---

## Configuratie (`.env`)

```
ALPACA_API_KEY=...        # PK... = paper, AK... = live
ALPACA_SECRET_KEY=...
ALPACA_PAPER=true         # false voor live
ALPACA_DATA_FEED=iex      # iex (gratis) of sip (betaald)
ANTHROPIC_API_KEY=...     # optioneel: schakelt Claude-sentiment in
SENTIMENT_MODEL=claude-opus-4-8   # optioneel; claude-haiku-4-5 is goedkoper voor bulk
ALLOW_LIVE_AUTOTRADE=false        # moet true zijn voor live auto-trading
```
