# Daily Stock Brief (static version)

GitHub Actions runs `fetch_data.py` on a schedule and publishes JSON files to the
`data` branch. The Netlify page reads those files. No server needed.

Schedule (see update-data.yml)
- Gold rate only:  every hour, all week (about 1 minute)
- Light refresh:   hourly 08:50-16:50 IST on weekdays (news, results, IPO, prices, gold)
- Full scan:       weekdays 16:15 IST (whole-market signals + fundamentals + dividends)
- By hand: Actions tab -> "Update market data" -> Run workflow -> choose full, light or gold.

IPO GMP: edit `ipo_gmp.json`, e.g. {"SOMEIPO": {"gmp": 25, "updated_at": "2026-11-20"}}
