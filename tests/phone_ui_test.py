"""Phone page end-to-end test: the real /crowd page in headless Chrome against a real v2 server.

Run from KittyAPI (inside the hackathon conda env), with Google Chrome installed:
    python tests/phone_ui_test.py

Starts uvicorn on port 8001 with a throwaway database and DEMO_MODE=1 (never port 8000, never the
v1 database), drives the page through the Chrome DevTools protocol (join with the 19+ box, wallet,
daily points, demo checkout, ticket entry, profile and cashier code, history, play-safe tools, out-of-points, reality check)
and prints PASS/FAIL per step. Chrome's profile, cache and the screenshots go under
<project>/.tmp/phone_ui (KITTY_TEST_TMP overrides). Set CHROME_PATH if Chrome is elsewhere.
"""
import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import websockets

API = Path(__file__).resolve().parent.parent
OUT = Path(os.getenv("KITTY_TEST_TMP") or API.parent / ".tmp") / "phone_ui"
OUT.mkdir(parents=True, exist_ok=True)
DB = OUT / f"phone-{time.strftime('%Y%m%d-%H%M%S')}.db"
CHROME = os.getenv("CHROME_PATH") or next(
    (p for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                 "/usr/bin/google-chrome",
                 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
     if Path(p).exists()),
    None,
)
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


async def main():
    if CHROME is None:
        sys.exit("Chrome not found; set CHROME_PATH")
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", 8001)) == 0:
            sys.exit("port 8001 is already in use; stop that server first")
    env = dict(os.environ, KITTY_DB_PATH=str(DB), DEMO_MODE="1", GAME_RNG_MODE="secure",
               TICKET_SECRET="phone-ui-test-ticket-key-0123456789")
    prefix = Path(sys.prefix)   # conda env DLLs (numpy/qiskit) when the env isn't activated
    env["PATH"] = os.pathsep.join([str(prefix), str(prefix / "Library" / "bin"), env.get("PATH", "")])
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--port", "8001", "--log-level", "warning"],
                              cwd=API, env=env, stdout=open(OUT / "server.log", "w"), stderr=subprocess.STDOUT)
    chrome = None
    try:
        for _ in range(120):
            try:
                if httpx.get("http://127.0.0.1:8001/api/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                await asyncio.sleep(0.5)
        chrome = subprocess.Popen([CHROME, "--headless=new", "--remote-debugging-port=9333",
                                   f"--user-data-dir={OUT / 'chrome-profile'}", f"--disk-cache-dir={OUT / 'chrome-cache'}",
                                   "--no-first-run", "--no-default-browser-check", "--window-size=420,900", "about:blank"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                tabs = httpx.get("http://127.0.0.1:9333/json", timeout=2).json()
                page = next(t for t in tabs if t["type"] == "page")
                break
            except Exception:
                await asyncio.sleep(0.5)

        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
            msg_id = 0

            async def cdp(method, **params):
                nonlocal msg_id
                msg_id += 1
                mine = msg_id
                await ws.send(json.dumps({"id": mine, "method": method, "params": params}))
                while True:
                    reply = json.loads(await ws.recv())
                    if reply.get("id") == mine:
                        return reply.get("result", {})

            async def js(expr):
                r = await cdp("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
                if "exceptionDetails" in r:
                    raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description", r["exceptionDetails"]))
                return r.get("result", {}).get("value")

            async def shot(name):
                data = (await cdp("Page.captureScreenshot", format="png"))["data"]
                (OUT / f"{name}.png").write_bytes(base64.b64decode(data))

            async def sheet_text():
                return await js("document.getElementById('sheet').classList.contains('hidden') ? '' : document.getElementById('sheetBody').innerText")

            await cdp("Page.enable")
            await cdp("Emulation.setDeviceMetricsOverride", width=400, height=860, deviceScaleFactor=1, mobile=True)
            await cdp("Page.navigate", url="http://127.0.0.1:8001/crowd")
            await asyncio.sleep(2)
            await js("localStorage.clear()")
            await cdp("Page.reload")
            await asyncio.sleep(2)

            # 19+ gate on join
            check("join disabled until 19+ ticked", await js("document.getElementById('joinBtn').disabled") is True)
            await shot("01_join")
            check("no panel open on the join screen", await js("document.getElementById('sheet').classList.contains('hidden')") is True)
            await js("document.getElementById('name').value='Tester'; const a=document.getElementById('ageOk'); a.checked=true; a.onchange(); document.getElementById('joinBtn').click()")
            await asyncio.sleep(1.5)
            check("joined (game view shown)", await js("!document.getElementById('gameView').classList.contains('hidden')") is True)
            pid = await js("localStorage.getItem('kc_pid')")
            limits = httpx.get("http://127.0.0.1:8001/api/wallet/limits", headers={"X-Session-ID": pid}).json()
            check("server recorded 19+", limits["age_confirmed"] is True)
            await asyncio.sleep(2)
            check("no panel open after joining", await js("document.getElementById('sheet').classList.contains('hidden')") is True)
            sprites = await js("Object.values(sheets).filter(i => i.complete && i.naturalWidth > 0).length")
            check("cat sprites load (no clash with the wallet code)", (sprites or 0) > 0, str(sprites))
            await shot("01b_game")

            # wallet
            await js("document.getElementById('walletBtn').click()")
            await asyncio.sleep(1)
            text = await sheet_text()
            check("wallet shows 50 points", "50 points" in text, text[:120])
            check("demo banner shown", "DEMO MODE" in text and "simulated" in text.lower())
            await shot("02_wallet")
            await js("claimDaily()")
            await asyncio.sleep(2)
            check("daily free -> 70", "70 points" in await sheet_text())

            # demo checkout
            await js("document.getElementById('tuAmt').value='7'; openTopup()")
            await asyncio.sleep(0.5)
            check("pay disabled before checkbox+method", await js("document.getElementById('payBtn').disabled") is True)
            await js("pickMethod('card')")
            check("pay still disabled without checkbox", await js("document.getElementById('payBtn').disabled") is True)
            await js("const c=document.getElementById('payOk'); c.checked=true; payReady()")
            check("pay enabled after checkbox", await js("document.getElementById('payBtn').disabled") is False)
            text = await sheet_text()
            check("checkout says simulated, no brand names", "SIMULATED" in text and not any(b in text for b in ("Apple", "Google")))
            await shot("03_checkout")
            await js("document.getElementById('payBtn').click()")
            await asyncio.sleep(1.5)
            text = await sheet_text()
            check("receipt shown ($7 -> +56, simulated)", "Receipt (simulated)" in text and "+56" in text and "$7.00" in text, text[:160])
            await shot("04_receipt")

            # ticket bonus: typed code, generic refusal, one a day
            await js("openWallet()")
            await asyncio.sleep(1)
            text = await sheet_text()
            check("ticket entry is a plain text box with a generic placeholder",
                  await js("document.getElementById('tCode').placeholder") == "Ticket code"
                  and await js("document.getElementById('tCode').type") == "text")
            await js("document.getElementById('tCode').value='hello'; redeemTicket()")
            await asyncio.sleep(1)
            message = await js("document.getElementById('tMsg').textContent")
            check("wrong code: generic message", message == "That ticket couldn't be verified", message)
            code = httpx.post("http://127.0.0.1:8001/api/demo/lottery/tickets").json()["code"]
            await js(f"document.getElementById('tCode').value={json.dumps(code)}; redeemTicket()")
            await asyncio.sleep(1)
            check("ticket redeemed +25", "+25 points" in await js("document.getElementById('tMsg').textContent"))
            await asyncio.sleep(2)   # the wallet sheet re-renders after a success
            check("one ticket a day", await js("document.querySelector('[onclick=\"redeemTicket()\"]').disabled") is True
                  and "tomorrow" in await js("document.getElementById('tMsg').textContent"))

            # profile + cashier code
            await js("openProfile()")
            await asyncio.sleep(1)
            await js("document.getElementById('pfName').value='Tess'; pickAvatar('calico'); saveProfile()")
            await asyncio.sleep(1)
            check("profile saved", "Saved as Tess" in await js("document.getElementById('pfMsg').textContent"))
            await js("showQr()")
            await asyncio.sleep(1)
            check("cashier QR shown", await js("!!document.querySelector('#qrBox svg')") is True)
            counter_code = await js("document.querySelector('#qrBox b').textContent")
            check("counter code shown", len(counter_code or "") == 9, counter_code)
            await shot("04b_profile_qr")

            # menu
            await js("openMenu()")
            await asyncio.sleep(1)
            text = await sheet_text()
            check("menu lists how to play, safe play, demo reset", "How to play" in text and "Safe play" in text and "New demo player" in text, text[:200])
            await shot("04c_menu")
            await js("openSafePlay()")
            await asyncio.sleep(0.5)
            text = await sheet_text()
            check("safe play page: plain advice + helpline", "no cash value" in text and "1-866-531-2600" in text and "9-8-8" in text, text[:200])
            await shot("04d_safe_play")
            await js("toggleLang(); openMenu()")
            await asyncio.sleep(0.5)
            text = await sheet_text()
            check("French menu", "Comment jouer" in text and "Jeu responsable" in text, text[:200])
            check("French bet button", (await js("document.getElementById('betBtn').textContent")).startswith("MISER"))
            await shot("04e_menu_fr")
            await js("openWallet()")
            await asyncio.sleep(1)
            text = await sheet_text()
            check("French wallet sheet", "Solde" in text and "UTILISER LE BILLET" in text and "Balance" not in text, text[:200])
            await js("document.getElementById('tCode').value='nope'; redeemTicket()")
            await asyncio.sleep(1)
            message = await js("document.getElementById('tMsg').textContent")
            check("French server message (daily limit, today's ticket is used)", message.startswith("Vous avez déjà utilisé le billet du jour"), message)
            await shot("04f_wallet_fr")
            await js("toggleLang(); closePanel()")

            # history
            await js("openHistory()")
            await asyncio.sleep(1)
            text = await sheet_text()
            check("history lists receipt and ledger", "Demo top-up" in text and "Ticket bonus" in text and "Daily free" in text, text[:200])
            await shot("05_history")

            # play safe
            await js("openSafe()")
            await asyncio.sleep(1)
            await js("document.getElementById('limitIn').value='30'; saveLimit(false)")
            await asyncio.sleep(1)
            check("limit set to 30 at once", "Now: 30" in await sheet_text())
            await js("document.getElementById('limitIn').value='90'; saveLimit(false)")
            await asyncio.sleep(1)
            text = await sheet_text()
            check("raise waits (pending shown)", "Now: 30" in text and "Change waiting: 90" in text, text[:200])
            await js("coolOff(1)")
            await asyncio.sleep(0.5)
            check("cool-off needs the checkbox", "Tick the box" in await js("document.getElementById('safeMsg').textContent"))
            await shot("06_safe")

            # out of points sheet (simulated empty wallet state)
            await js("closePanel(); walletOnState({phase:'betting', round_no: 999, you:{bet:0, balance:0}})")
            await asyncio.sleep(1)
            check("out-of-points sheet", "Out of points" in await sheet_text())
            await shot("07_out_of_points")

            # reality check fires when due
            await js("closePanel(); rcNext = Date.now() - 1")
            for _ in range(25):
                await asyncio.sleep(1)
                if "Reality check" in await sheet_text():
                    break
            text = await sheet_text()
            check("reality check shows activity", "Reality check" in text and "Playing for" in text, text[:120])
            await shot("08_reality_check")

            # cool-off really blocks bets (server side)
            await js("openSafe()")
            await asyncio.sleep(1)
            await js("document.getElementById('coolOk').checked=true; coolOff(1)")
            await asyncio.sleep(1)
            check("break shown after cool-off", "Taking a break until" in await sheet_text())

            # cashier page (same computer) tops up with the counter code, once
            await cdp("Emulation.setDeviceMetricsOverride", width=900, height=900, deviceScaleFactor=1, mobile=False)
            await cdp("Page.navigate", url="http://127.0.0.1:8001/cashier")
            await asyncio.sleep(1.5)
            await js(f"document.getElementById('code').value={json.dumps(counter_code)}; document.getElementById('lookup').click()")
            await asyncio.sleep(1)
            check("cashier finds the player", await js("document.getElementById('nickname').textContent") == "Tess")
            await js("document.getElementById('amount').value='3'; updatePoints(); const c=document.getElementById('confirm'); c.checked=true; updatePoints(); document.getElementById('topup').click()")
            await asyncio.sleep(1)
            msg = await js("document.getElementById('topupMsg').textContent")
            check("cashier credit refused during the break", "cooling off" in msg.lower(), msg)
            other = httpx.post("http://127.0.0.1:8001/api/sessions").json()["session_id"]
            httpx.post("http://127.0.0.1:8001/api/wallet/age-confirm", headers={"X-Session-ID": other}, json={"confirm": True})
            other_code = httpx.post("http://127.0.0.1:8001/api/profile/qr", headers={"X-Session-ID": other}).json()["counter_code"]
            await js("document.getElementById('cancel').click()")
            await js(f"document.getElementById('code').value={json.dumps(other_code)}; document.getElementById('lookup').click()")
            await asyncio.sleep(1)
            await js("document.getElementById('amount').value='3'; const k=document.getElementById('confirm'); k.checked=true; updatePoints(); document.getElementById('topup').click()")
            await asyncio.sleep(1)
            msg = await js("document.getElementById('topupMsg').textContent")
            check("cashier credits $3 = 24 points", "Credited 24 points" in msg, msg)
            await shot("09_cashier")
            await js(f"document.getElementById('code').value={json.dumps(other_code)}; document.getElementById('lookup').click()")
            await asyncio.sleep(1)
            msg = await js("document.getElementById('lookupMsg').textContent")
            check("cashier code works once", "already used" in msg, msg)
    finally:
        if chrome:
            chrome.terminate()
        server.terminate()
        server.wait(10)
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)


asyncio.run(main())
