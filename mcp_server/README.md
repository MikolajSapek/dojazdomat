# Dojazdomat MCP

Zdalny serwer MCP dla Dojazdomat (Warszawa: dojazd x czynsz). Narzędzia: `find_place`, `commute_time`, `rent_stats`, `rank_neighborhoods`, `rank_for_two`, `compare_neighborhoods`, `reachable_neighborhoods`, `rent_trend`, `dataset_info`. Czasy dojazdu z domu do pracy w trybach `transit`, `bike`, `walk`, `car` (parametr `mode`, te same minuty co mapa); `rent_trend` czyta `web/data/rent-history.json` (`pipeline/rent_history.py`, co noc). Zwraca wyłącznie agregaty (mediana, p25, p75, liczebność; cena dopiero od 20 ogłoszeń, p25 i p75 od 10, promień tylko ze stałych kroków); nigdy pojedynczych ogłoszeń ani ich współrzędnych.

Dane i reguły jak na mapie (`offers.json`, próg 20 ogłoszeń do rankingu osiedla); `rank_neighborhoods` ma parametr `sort` (`value`, `best`, `commute`, `rent`).

Zalecany przepływ: `find_place` -> `rank_neighborhoods` -> `rent_stats`.

## Uruchomienie

```bash
cd <repo>
.venv/bin/python -m mcp_server --port 8790        # HTTP: http://127.0.0.1:8790/mcp (host: --host or HOST)
.venv/bin/python -m mcp_server --stdio            # stdio (lokalnie)
```

Zmienne: `DOJAZDOMAT_DATA` (domyślnie `web/data`), `DOJAZDOMAT_RATE_PER_MIN` (domyślnie 60 zapytań/min/IP, nagłówek `CF-Connecting-IP`), `DOJAZDOMAT_ALLOWED_HOSTS` (lista po przecinku, np. `dojazdomat.pl`; włącza ochronę DNS-rebinding, domyślnie wyłączona).

Docker (z katalogu repo): `docker build -f mcp_server/Dockerfile -t dojazdomat-mcp . && docker run -p 8790:8790 dojazdomat-mcp`. Tunel Cloudflare: `dojazdomat.pl` -> `http://localhost:8790`.

## Podłączenie

**Claude (claude.ai):** Settings -> Connectors -> Add custom connector, URL `https://dojazdomat.pl/mcp`, bez uwierzytelniania.

**Claude Code:**
```bash
claude mcp add --transport http dojazdomat https://dojazdomat.pl/mcp
```

**Codex** (`~/.codex/config.toml`):
```toml
[mcp_servers.dojazdomat]
url = "https://dojazdomat.pl/mcp"
```
Wersja lokalna przez stdio:
```toml
[mcp_servers.dojazdomat]
command = "<repo>/.venv/bin/python"
args = ["-m", "mcp_server", "--stdio"]
cwd = "<repo>"
```

**ChatGPT:** Settings -> Apps & Connectors -> Advanced -> Developer mode, potem Create (custom connector): URL `https://dojazdomat.pl/mcp`, uwierzytelnianie: brak.

**Claude Desktop (stdio)**, `claude_desktop_config.json`:
```json
{"mcpServers": {"dojazdomat": {
  "command": "<repo>/.venv/bin/python",
  "args": ["-m", "mcp_server", "--stdio"],
  "cwd": "<repo>"}}}
```

## Dane i zastrzeżenia

Ceny ofertowe (nie transakcyjne), czynsz administracyjny wliczony, gdy podany osobno. Morizon pokrywa się z Gratką, portale nie są deduplikowane między sobą. Ogłoszenia z `coordPrecision` 2 są pomijane. Dojazd: komunikacja miejska + pieszo, dni robocze 7-9. Szczegóły: `dataset_info`.

Testy: `.venv/bin/pytest -q tests/test_mcp.py`. SDK: `mcp==1.30.0` (seria 1.x; 2.x zmienia API `FastMCP` na `MCPServer`).
