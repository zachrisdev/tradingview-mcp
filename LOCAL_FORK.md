# Local fork — Claude Desktop

This repo ([zachrisdev/tradingview-mcp](https://github.com/zachrisdev/tradingview-mcp)) is a fork of [atilaahmettaner/tradingview-mcp](https://github.com/atilaahmettaner/tradingview-mcp). Point Claude Desktop at the **local clone** so fundamental screener filters take effect (not the PyPI `uvx` package).

**Branch:** `feat/fundamental-stock-screener`  
**Change:** `stock_screener` optional AND-combined fundamental filters, plus
2026-10-13 incubator fields: gross margin TTM, revenue TTM, FQ YoY,
FCF-margin / Rule of 40 (computed), `industry`, `is_primary` / `primary_only`,
heuristic `market_cap_usd` + USD post-filters (over-fetch pipeline).

**Remotes (local clone):**
- `origin` → your fork
- `upstream` → `https://github.com/atilaahmettaner/tradingview-mcp.git`

### Claude Desktop config

File: `%APPDATA%\Claude\claude_desktop_config.json`

**PyPI / uvx (legacy):**
```json
"tradingview": {
  "command": "uvx",
  "args": ["--python", "3.13", "--from", "tradingview-mcp-server", "tradingview-mcp"],
  "env": { "MARKETAUX_API_TOKEN": "..." }
}
```

**Local fork binding** (keep your MARKETAUX token):
```json
"tradingview": {
  "command": "uv",
  "args": ["run", "--directory", "E:/Utils/_MCP/tradingview-mcp", "--python", "3.13", "tradingview-mcp"],
  "env": { "MARKETAUX_API_TOKEN": "<keep existing>" }
}
```

(`uv` / `uvx` should be on PATH, or use the full path under `%USERPROFILE%\\.local\\bin\\`.)

After changing the Claude Desktop config, **restart the app**.

**Note:** unofficial TradingView scanner API — fragile.
