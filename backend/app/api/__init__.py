"""
HTTP API of the PocketDesk backend (the contract is `docs/API.md`; the web client is `web/lib/api.ts`).

One router per area, all mounted by `app/main.py:create_app`:

    health.py    GET  /api/health, /api/config
    state.py     GET  /api/state
    feeds.py     GET  /api/purchases, /api/findings, /api/allocations, /api/events-today,
                      /api/pm-vs-options, /api/market, /api/alerts
    budget.py    POST /api/budget/{prepare,open,prepare-pause,pause,prepare-revoke,revoke}
    vouchers.py  POST /api/vouchers/{prepare,submit,prepare-cancel,cancel}, GET /api/vouchers
    session.py   POST /api/session/{start,stop,tick}
    demo.py      GET  /api/demo/status, POST /api/demo/fund (server-side demo wallet, testnet/anvil only)
    agents.py    POST /api/agents/{name}/{pause,resume}
    ask.py       POST /api/ask
    catalog.py   GET  /api/catalog, /api/x402-probe
    settings.py  GET/POST /api/settings
    stream.py    GET  /api/stream (Server-Sent Events)
    faucet.py    POST /api/faucet
    breaker.py   GET  /api/breaker

Every handler is short: it validates the request, asks the keeper `Runtime` (or the chain
clients it holds) to do the work, and returns the JSON shape of docs/API.md. Shared plumbing
(getting the runtime, resolving the owner, turning chain errors into `{detail}` answers) lives
in `deps.py`. No endpoint ever returns or logs a private key.
"""
