# Putting Catino online

Catino is a points-only game. Nothing here moves real money. These are the ways to give the phone page and the
join QR a public https address, from quickest to most permanent.

**Always set `PUBLIC_MODE=1`** on anything reachable from the internet. It
- closes `/cashier`, `/operator` and the demo helpers completely (a tunnel forwards from 127.0.0.1, so "local" proves nothing);
- lets only the game on the server computer take the crowd host seat;
- turns on per-visitor rate limits (wallet creation, join, bets, seeds), a request size cap and a WebSocket limit;
- keeps simulated purchases off unless you also set `DEMO_MODE=1` **and** `ALLOW_PUBLIC_DEMO=1`.

Run **one** worker. The crowd table and the rounds live in memory.

## 1. Your PC plus a tunnel (about 10 minutes, free)

Good for a live demo. The link works while your PC, the server and the tunnel are running.

1. Install `cloudflared` (no account needed) or `ngrok` (free account, one-time auth token).
2. Double-click `start_tunnel.bat` and copy the `https://...` address it prints.
3. Double-click `start_public.bat` and paste the address when asked. The server listens on `127.0.0.1` only,
   so just the tunnel can reach it.
4. In Unity: Kitty Crash > Server > Use v2 Server (it stays `http://127.0.0.1:8001`), tick Use Api, Play, press **C**.
   The QR now holds the public address, so phones anywhere can join.
5. Open `<address>/crowd` on a phone to check it. The cashier is closed in this mode.

A free quick-tunnel address changes every time. For a fixed address use a named Cloudflare tunnel or an ngrok
reserved domain, and put `PUBLIC_URL=...` in `KittyAPI\.env.local`.

## 2. A container host (Render, Fly.io, Railway)

`KittyAPI` has a `Dockerfile`, `render.yaml` and `fly.toml`.

- **Persistent disk:** mount a volume at `/data` (the image sets `KITTY_DB_PATH=/data/catino-v2.db`). Without one, wallets reset
  when the service restarts. Render's disk needs a paid plan; Fly volumes are cheap; free tiers also sleep when idle.
- **Secrets:** set `TICKET_SECRET` and `QR_SECRET` (long random values) and `PUBLIC_URL=https://<your host>`.
- **Health check:** `/api/health`.
- A round needs a host to open betting. With only the hosted server online, the phones can join but the table stays
  idle until a host (the Unity game pointed at the hosted address) opens a round. An automatic host is listed below.

## 3. A VM (AWS Lightsail/EC2, any VPS)

```
sudo apt install python3-pip caddy git
git clone <repo> && cd KittyAPI
pip install -r requirements.txt
export PUBLIC_MODE=1 PUBLIC_URL=https://catino.example.com TICKET_SECRET=... QR_SECRET=...
uvicorn main:app --host 127.0.0.1 --port 8001 --proxy-headers --forwarded-allow-ips=127.0.0.1
```

Put the `Caddyfile` in front (it gets a free certificate for your domain and sends `X-Forwarded-For`).
Open only ports 80 and 443. Set a billing alert and stop the machine when you are done.

## The proxy must forward the visitor's address

The limits and the host/cashier protection rely on the proxy sending `X-Forwarded-For` or `CF-Connecting-IP`.
cloudflared, ngrok, Caddy (`reverse_proxy`), Render, Fly and Railway all do. A hand-written nginx needs
`proxy_set_header X-Forwarded-For $remote_addr;`. Never expose the server directly (`0.0.0.0`) in public mode
without a proxy in front.

## Backups and resets

```
python tools/db_tools.py backup     # safe while running; written next to the database in backups/
python tools/db_tools.py reset      # stop the server first; the old file is kept, the next start makes a fresh one
python tools/db_tools.py list
```

On a host, run these through its shell (`fly ssh console`, Render shell). Back up before each demo.

## Before a live demo

- `<address>/api/health` shows the right version and `"demo_mode": false`.
- `<address>/cashier` and `<address>/operator` answer 403.
- Join from two phones on mobile data (not your Wi-Fi), play a round, and open the Schrödinger's Box on each.
- Take a backup, and reset afterwards if you want a clean table.

## Not built yet

- An automatic host, so a hosted table opens rounds without the Unity game.
- Real logins and identity checks (wallets are anonymous and 19+ is self-attested).
