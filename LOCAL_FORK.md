# Local fork — Claude Desktop

## HU

Ez a repo ([zachrisdev/tradingview-mcp](https://github.com/zachrisdev/tradingview-mcp)) az [atilaahmettaner/tradingview-mcp](https://github.com/atilaahmettaner/tradingview-mcp) forkja. A Claude Desktopot a **helyi klónra** kell irányítani, hogy a fundamentális screener filterek érvényesüljenek (nem a PyPI `uvx` csomagra).

**Ág:** `feat/fundamental-stock-screener`  
**Újdonság:** a `stock_screener` opcionális AND-fundamentális szűrőket támogat.

**Remotes (helyi klón):**
- `origin` → `https://github.com/zachrisdev/tradingview-mcp.git`
- `upstream` → `https://github.com/atilaahmettaner/tradingview-mcp.git`

### Claude Desktop config

Fájl: `%APPDATA%\Claude\claude_desktop_config.json`

**PyPI / uvx (régi):**
```json
"tradingview": {
  "command": "C:\\Users\\zacharfrisztian\\.local\\bin\\uvx.exe",
  "args": ["--python", "3.13", "--from", "tradingview-mcp-server", "tradingview-mcp"],
  "env": { "MARKETAUX_API_TOKEN": "..." }
}
```

**Helyi fork kötés** (a MARKETAUX tokent tartsd meg):
```json
"tradingview": {
  "command": "C:\\Users\\zacharfrisztian\\.local\\bin\\uv.exe",
  "args": ["run", "--directory", "E:\\Utils\\_MCP\\tradingview-mcp", "--python", "3.13", "tradingview-mcp"],
  "env": { "MARKETAUX_API_TOKEN": "<keep existing>" }
}
```

**Alternatíva** (`uv` a PATH-on):
```json
"tradingview": {
  "command": "uv",
  "args": ["run", "--directory", "E:/Utils/_MCP/tradingview-mcp", "tradingview-mcp"],
  "env": { "MARKETAUX_API_TOKEN": "<keep existing>" }
}
```

Config váltás után **indítsd újra a Claude Desktopot**.

**Figyelem:** nem hivatalos TradingView scanner API — törékeny lehet.

---

## EN

This repo ([zachrisdev/tradingview-mcp](https://github.com/zachrisdev/tradingview-mcp)) is a fork of [atilaahmettaner/tradingview-mcp](https://github.com/atilaahmettaner/tradingview-mcp). Point Claude Desktop at the **local clone** so fundamental screener filters take effect (not the PyPI `uvx` package).

**Branch:** `feat/fundamental-stock-screener`  
**Change:** `stock_screener` now has optional AND-combined fundamental filters.

**Remotes (local clone):**
- `origin` → your fork
- `upstream` → upstream project

After changing the Claude Desktop config, **restart the app**.

**Note:** unofficial TradingView scanner API — fragile.
