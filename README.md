# Situational Awareness Scanner

Automatisiertes CALL-Options Intelligence System basierend auf Aschenbrenners
„The Decade Ahead". Identifiziert handelbare CALL-Optionen auf KI-Infrastruktur
durch Kombination von 13F-Monitoring, philosophisch-strategischer These-Analyse
(Thiel, Shulman) und Marktdaten.

## Architektur

```
main.py                         Orchestrierung
scanner/
  sources/
    tradier_client.py           Tradier Vollzugriff (Option Chain, Greeks, IV-Historie)
    data_fetcher.py             Koordiniert alle Quellen (yfinance, EIA, FRED, Finnhub, RSS)
    sec_edgar.py                13F-Filing Monitor
  signals/
    regime_detector.py          Normal/Stress-Modus Bestimmung
    contrarian_gate.py          Gegenthesen-Check (verhindert Überzeugungsschleifen)
    shulman_layer.py            Empirische Validierung + qualitative Extraktion
    thiel_layer.py              Handlung vs. These + Katechon-Bonus
  analysis/
    pre_filter.py               Quick-Score ohne Claude (Token-Ökonomie)
    scoring_engine.py           Gewichteter Conviction-Score
    claude_analyzer.py          Anthropic API + Master-Prompt
  output/
    trading_card_generator.py   JSON → HTML Trading Card
    dashboard_generator.py      GitHub Pages Dashboard
  utils/
    config.py                   Alle Konstanten und Gewichte
    state_manager.py            SQLite + Git-Commit Persistenz
    ticker_mapper.py            CIK → Ticker → Sektor Mapping
    rate_limiter.py             Pro-API Rate-Limiting
```

## Setup

### 1. Repository

```bash
git clone https://github.com/DEIN-USER/sa-scanner
cd sa-scanner
pip install -r requirements.txt
```

### 2. GitHub Secrets

Unter `Settings → Secrets and variables → Actions`:

| Secret | Quelle | Pflicht |
|--------|--------|---------|
| `ANTHROPIC_API_KEY` | console.anthropic.com | ✅ |
| `TRADIER_API_KEY` | tradier.com/user/applications | ✅ |
| `FINNHUB_API_KEY` | finnhub.io | ✅ |
| `EIA_API_KEY` | eia.gov/opendata | Empfohlen |
| `FRED_API_KEY` | fred.stlouisfed.org | Empfohlen |

### 3. GitHub Actions Schreibrechte

`Settings → Actions → General → Workflow permissions → Read and write permissions`

### 4. GitHub Pages

`Settings → Pages → Source → GitHub Actions`

### 5. CIK-Nummern verifizieren

In `scanner/utils/config.py` die `SEC_CIK_TARGETS` über
[EDGAR Company Search](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany)
verifizieren.

### 6. Lokaler Test

```bash
# Nur Daten fetchen (kein Claude)
python main.py --no-claude

# Spezifische Ticker
python main.py --ticker VST PLTR

# Nur EDGAR-Check
python main.py --edgar-only

# Tests
pytest tests/ -v
```

## Gewichtungsstruktur

| Layer | Normal | Stress |
|-------|--------|--------|
| SA LP Alignment | 40% | 50% |
| Thiel (inkl. Philosophical) | 14% | 10% |
| Shulman-Metriken | 15% | 13% |
| Multi-Signal-Gate | 4% | 3% |
| Markt-Regime | 15% | 12% |
| Contrarian Gate | 12% | 12% |

**Conviction-Schwellenwert:** ≥ 7.5 (Normal) / ≥ 8.0 (Stress)

**Contrarian Gate:** Bei Score < -3.0 wird der Trade blockiert (binäres Gate).

## Kosten

| Quelle | Kosten |
|--------|--------|
| Anthropic API (claude-sonnet-4-6) | ~2-3 USD/Monat (Pre-Filter) |
| Tradier (Vollzugriff) | Laut Plan |
| Alle anderen Quellen | kostenlos |

## Wichtige Einschränkungen

Das System liefert **Richtungs-Signale**, keine Timing-Garantien.

- Philosophische Signale (Thiel-These) sind Priors, keine Trigger
- Shulman-Signale liegen 6-12 Monate vor dem Mainstream-Markt
- IV-Rank ist erst nach 30+ Tagen eigener Datensammlung zuverlässig (Warmup-Phase)
- Der Contrarian Gate ist der einzige Schutz gegen geschlossene Überzeugungsschleifen
- Nicht investiere niemals mehr als du bereit bist zu verlieren

## Lizenz

Privat — nicht für kommerzielle Weitergabe.
