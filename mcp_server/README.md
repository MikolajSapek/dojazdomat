# Dojazdomat MCP

Zdalny serwer MCP dla Dojazdomat (Warszawa: dojazd x czynsz). Narzędzia: `find_place`, `commute_time`, `rent_stats`, `rank_neighborhoods`, `dataset_info`. Zwraca wyłącznie agregaty (mediana, p25, p75, liczebność z grup >= 5 ogłoszeń); nigdy pojedynczych ogłoszeń ani ich współrzędnych.

Zalecany przepływ: `find_place` -> `rank_neighborhoods` -> `rent_stats`.

## Uruchomienie

```bash
cd dojazdomat
.venv/bin/python -m mcp_server --port 8790        # HTTP: http://0.0.0.0:8790/mcp
.venv/bin/python -m mcp_server --stdio            # stdio (lokalnie)
```

Zmienne: `DOJAZDOMAT_DATA` (domyślnie `web/data`), `DOJAZDOMAT_RATE_PER_MIN` (domyślnie 60 zapytań/min/IP, nagłówek `CF-Connecting-IP`), `DOJAZDOMAT_ALLOWED_HOSTS` (lista po przecinku, np. `dojazdomat.sapcorp.eu`; włącza ochronę DNS-rebinding, domyślnie wyłączona).

Docker (z katalogu repo): `docker build -f mcp_server/Dockerfile -t dojazdomat-mcp . && docker run -p 8790:8790 dojazdomat-mcp`. Tunel Cloudflare: `dojazdomat.sapcorp.eu` -> `http://localhost:8790`.

## Podłączenie

**Claude (claude.ai):** Settings -> Connectors -> Add custom connector, URL `https://dojazdomat.sapcorp.eu/mcp`, bez uwierzytelniania.

**Claude Code:**
```bash
claude mcp add --transport http dojazdomat https://dojazdomat.sapcorp.eu/mcp
```

**Codex** (`~/.codex/config.toml`):
```toml
[mcp_servers.dojazdomat]
url = "https://dojazdomat.sapcorp.eu/mcp"
```
Wersja lokalna przez stdio:
```toml
[mcp_servers.dojazdomat]
command = "/ścieżka/do/dojazdomat/.venv/bin/python"
args = ["-m", "mcp_server", "--stdio"]
cwd = "/ścieżka/do/dojazdomat"
```

**ChatGPT:** Settings -> Apps & Connectors -> Advanced -> Developer mode, potem Create (custom connector): URL `https://dojazdomat.sapcorp.eu/mcp`, uwierzytelnianie: brak.

**Claude Desktop (stdio)**, `claude_desktop_config.json`:
```json
{"mcpServers": {"dojazdomat": {
  "command": "/ścieżka/do/dojazdomat/.venv/bin/python",
  "args": ["-m", "mcp_server", "--stdio"],
  "cwd": "/ścieżka/do/dojazdomat"}}}
```

## Dane i zastrzeżenia

Ceny ofertowe (nie transakcyjne), czynsz administracyjny wliczony, gdy podany osobno. Morizon pokrywa się z Gratką, portale nie są deduplikowane między sobą. Ogłoszenia z `coordPrecision` 2 są pomijane. Dojazd: komunikacja miejska + pieszo, dni robocze 7-9. Szczegóły: `dataset_info`.

Testy: `.venv/bin/pytest -q tests/test_mcp.py`. SDK: `mcp==1.30.0` (seria 1.x; 2.x zmienia API `FastMCP` na `MCPServer`).
