"""Telegram-бот: мониторит 8 пар на M5 и присылает сигналы по стратегии SMA20/SMA50.

Для каждого сигнала бот пишет:
  - почему заход (тренд, тест синей линии, сигнальная свеча) с цифрами;
  - готовую строку для таблицы Investor diary (колонки через табуляцию, вставляется в Excel в ячейки).
Потом следит за ценой и сообщает, когда позиция ушла в минимальный плюс (можно закрывать),
и присылает вторую строку уже с ценой выхода и прибылью.

Установка:  pip install yfinance pandas requests
Запуск:     python tg_signal_bot.py
Токен:      создай бота у @BotFather, вставь токен в TOKEN ниже или задай переменную окружения TG_TOKEN.
            Потом открой своего бота в Telegram и отправь ему /start.

Команды бота: /start /status /list /skip EURUSD /help
ВАЖНО: цены и время в сообщениях ориентировочные (данные Yahoo, время МСК).
В таблицу вноси реальные цену и время сделки из MT5.
"""
import html
import json
import os
import time
from datetime import datetime, timedelta

import pandas as pd
import requests
import yfinance as yf

TOKEN = os.getenv("TG_TOKEN") or "PASTE_TOKEN_HERE"
INTERVAL = "5m"
BAR_MIN = 5
FAST, SLOW = 20, 50          # синяя и красная
POLL_SECONDS = 20
MIN_PIPS = 1.0               # во сколько пипсов профита сообщать "можно закрывать"
STATE_FILE = "state.json"
TZ = "Europe/Moscow"
RUN_ONCE = bool(os.getenv("RUN_ONCE"))   # режим GitHub Actions: один проход и выход
MAX_DELAY_MIN = 20 if RUN_ONCE else 2 * BAR_MIN   # насколько старый сигнал ещё отправляем

SYMBOLS = {
    "AUDUSD": "AUDUSD=X",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "NZDUSD": "NZDUSD=X",
    "USDCHF": "USDCHF=X",
    "USDJPY": "USDJPY=X",
    "XAUUSD": "GC=F",        # фьючерс золота: цена отличается от XAUUSD в MT5
    "USDCAD": "USDCAD=X",
}
PIP = {"USDJPY": 0.01, "XAUUSD": 0.1}   # размер пипса, по умолчанию 0.0001
DECIMALS = {"USDJPY": 3, "XAUUSD": 2}   # по умолчанию 5


def pip(sym):
    return PIP.get(sym, 0.0001)


def fmt_price(sym, x):
    return f"{x:.{DECIMALS.get(sym, 5)}f}"


# ---------- состояние ----------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"chat_id": None, "counter": 0, "last_signal": {}, "open": {}, "offset": 0}


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


# ---------- telegram ----------
def tg(method, **data):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data, timeout=15)
    return r.json()


def send(st, text):
    if not st.get("chat_id"):
        print("[нет chat_id, отправь боту /start]\n" + text)
        return
    res = tg("sendMessage", chat_id=st["chat_id"], text=text, parse_mode="HTML")
    if not res.get("ok"):
        print("Telegram error:", res)


# ---------- данные и стратегия ----------
def fetch(ysym):
    df = yf.download(ysym, period="5d", interval=INTERVAL, progress=False, auto_adjust=False)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close"]].dropna()
    df.index = df.index.tz_localize("UTC") if df.index.tz is None else df.index
    df.index = df.index.tz_convert(TZ)
    df["fast"] = df["Close"].rolling(FAST).mean()
    df["slow"] = df["Close"].rolling(SLOW).mean()
    return df


def find_signal(df):
    """Прогон по закрытым свечам. Сигнал возвращается, только если он на последней свече df."""
    state, test = 0, None
    for i in range(SLOW, len(df)):
        r = df.iloc[i]
        f, s = r["fast"], r["slow"]
        sig = None
        if f > s:
            if state == -1:
                state = 0
            if r["Low"] <= f:
                state, test = 1, dict(time=df.index[i], extreme=r["Low"], ma=f)
            elif state == 1 and r["Close"] > r["Open"]:
                sig = dict(side="BUY", test=test, time=df.index[i], f=f, s=s, o=r["Open"],
                           c=r["Close"], h=r["High"], l=r["Low"])
                state = 0
        elif f < s:
            if state == 1:
                state = 0
            if r["High"] >= f:
                state, test = -1, dict(time=df.index[i], extreme=r["High"], ma=f)
            elif state == -1 and r["Close"] < r["Open"]:
                sig = dict(side="SELL", test=test, time=df.index[i], f=f, s=s, o=r["Open"],
                           c=r["Close"], h=r["High"], l=r["Low"])
                state = 0
        else:
            state = 0
        if sig and i == len(df) - 1:
            return sig
    return None


def hhmm(t):
    return t.strftime("%d.%m %H:%M")


def explain(sym, g):
    p = lambda x: fmt_price(sym, x)
    t = g["test"]
    if g["side"] == "BUY":
        return (f"Тренд вверх: SMA{FAST} (синяя) {p(g['f'])} выше SMA{SLOW} (красная) {p(g['s'])}. "
                f"Тест: свеча {hhmm(t['time'])} опустилась до синей линии (low {p(t['extreme'])} ≤ SMA{FAST} {p(t['ma'])}) и оттолкнулась. "
                f"Сигнальная свеча {hhmm(g['time'])} зелёная (open {p(g['o'])} → close {p(g['c'])}) "
                f"и целиком выше синей линии (low {p(g['l'])} > SMA{FAST} {p(g['f'])}).")
    return (f"Тренд вниз: красная SMA{SLOW} {p(g['s'])} выше синей SMA{FAST} {p(g['f'])}. "
            f"Тест: свеча {hhmm(t['time'])} поднялась до синей линии (high {p(t['extreme'])} ≥ SMA{FAST} {p(t['ma'])}) и оттолкнулась. "
            f"Сигнальная свеча {hhmm(g['time'])} красная (open {p(g['o'])} → close {p(g['c'])}) "
            f"и целиком ниже синей линии (high {p(g['h'])} < SMA{FAST} {p(g['f'])}).")


def test_text(g):
    t = g["test"]
    if g["side"] == "BUY":
        return (f"Buy: касание SMA{FAST} сверху ({hhmm(t['time'])}), отскок, "
                f"первая зелёная свеча выше линии ({hhmm(g['time'])})")
    return (f"Sell: касание SMA{FAST} снизу ({hhmm(t['time'])}), отскок, "
            f"первая красная свеча ниже линии ({hhmm(g['time'])})")


def row(n, date, sym, test, entry_time, entry_price, exit_price="", profit="", reason=""):
    return "\t".join(str(x) for x in [n, date, sym, test, entry_time, entry_price, exit_price, profit, reason])


# ---------- логика бота ----------
def on_signal(st, sym, g, price_now, now):
    st["counter"] += 1
    n = st["counter"]
    p = fmt_price(sym, price_now)
    st["open"][sym] = dict(n=n, side=g["side"], entry=price_now, time=now.isoformat(),
                           test=test_text(g), date=now.strftime("%d.%m.%Y"))
    emoji = "🟢" if g["side"] == "BUY" else "🔴"
    line = row(n, now.strftime("%d.%m.%Y"), sym, test_text(g), now.strftime("%H:%M"), p)
    send(st, f"{emoji} <b>{g['side']} {sym}</b> (M5)\n\n<b>Почему заход:</b>\n{html.escape(explain(sym, g))}\n\n"
             f"Цена сейчас ≈ {p}\n\n<b>Строка для таблицы (вход):</b>\n<pre>{html.escape(line)}</pre>\n"
             f"Закрывать, когда сделка в плюсе. Скажу, когда цена уйдёт на +{MIN_PIPS:g} пипс.")


def on_exit(st, sym, pos, price_now, profit_pips, now):
    p = fmt_price(sym, price_now)
    reason = f"Достигнут минимальный профит (+{profit_pips:.1f} пипса)"
    t0 = datetime.fromisoformat(pos["time"])
    line = row(pos["n"], pos["date"], sym, pos["test"], t0.strftime("%H:%M"),
               fmt_price(sym, pos["entry"]), p, f"+{profit_pips:.1f} pips", reason)
    send(st, f"✅ <b>{pos['side']} {sym}: можно закрывать</b>\nЦена ≈ {p}, профит ≈ +{profit_pips:.1f} пипса "
             f"(спред брокера смотри в MT5, профит считай по факту).\n\n"
             f"<b>Строка для таблицы (вход + выход):</b>\n<pre>{html.escape(line)}</pre>")


def process_symbol(st, sym, ysym):
    df = fetch(ysym)
    if df is None or len(df) < SLOW + 5:
        print(f"{sym}: НЕТ ДАННЫХ от Yahoo (строк: {0 if df is None else len(df)})")
        return
    now = datetime.now(df.index.tz)
    print(f"{sym}: свечей {len(df)}, последняя {df.index[-1].strftime('%d.%m %H:%M')}, "
          f"SMA20={df['fast'].iloc[-2]:.5f} SMA50={df['slow'].iloc[-2]:.5f}")
    price_now = float(df["Close"].iloc[-1])
    closed = df.iloc[:-1]
    # выход
    pos = st["open"].get(sym)
    if pos:
        diff = price_now - pos["entry"] if pos["side"] == "BUY" else pos["entry"] - price_now
        if diff / pip(sym) >= MIN_PIPS:
            on_exit(st, sym, pos, price_now, diff / pip(sym), now)
            del st["open"][sym]
        return
    # вход
    g = find_signal(closed)
    if not g:
        print(f"{sym}: сигнала нет")
        return
    close_time = g["time"] + timedelta(minutes=BAR_MIN)
    if now - close_time > timedelta(minutes=MAX_DELAY_MIN):
        return  # старый сигнал (например, после перезапуска бота)
    if st["last_signal"].get(sym) == g["time"].isoformat():
        return
    st["last_signal"][sym] = g["time"].isoformat()
    on_signal(st, sym, g, price_now, now)


def handle_commands(st):
    res = tg("getUpdates", offset=st["offset"], timeout=0)
    for u in res.get("result", []):
        st["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        cid = msg["chat"]["id"]
        cmd = text.split()
        if cmd[0].startswith("/start"):
            st["chat_id"] = cid
            tg("sendMessage", chat_id=cid, text=(
                "Готово, буду присылать сигналы по 8 парам на M5 (SMA20/SMA50).\n"
                "Время в сообщениях — МСК, цены с Yahoo (ориентировочные): в таблицу вноси реальные данные сделки из MT5.\n"
                "/status — состояние, /list — открытые, /skip EURUSD — убрать пару из отслеживания выхода."))
        elif cmd[0].startswith("/list"):
            tg("sendMessage", chat_id=cid, text="\n".join(
                f"{s}: {p['side']} с {fmt_price(s, p['entry'])} (№{p['n']})" for s, p in st["open"].items()) or "Открытых нет")
        elif cmd[0].startswith("/skip") and len(cmd) > 1:
            s = cmd[1].upper()
            tg("sendMessage", chat_id=cid, text=f"{s}: убрал" if st["open"].pop(s, None) else f"{s}: не отслеживается")
        elif cmd[0].startswith("/status"):
            tg("sendMessage", chat_id=cid, text=f"Работаю. Сигналов всего: {st['counter']}, отслеживается выходов: {len(st['open'])}.")
        elif cmd[0].startswith("/help"):
            tg("sendMessage", chat_id=cid, text="/start /status /list /skip ТИКЕР")
    save_state(st)


if __name__ == "__main__":
    if TOKEN == "PASTE_TOKEN_HERE":
        raise SystemExit("Вставь токен бота в TOKEN или задай переменную TG_TOKEN")
    st = load_state()

    def one_pass():
        try:
            handle_commands(st)
        except Exception as e:
            print("commands error:", e)
        for sym, ysym in SYMBOLS.items():
            try:
                process_symbol(st, sym, ysym)
            except Exception as e:
                print(datetime.now().strftime("%H:%M:%S"), sym, "error:", e)
        save_state(st)

    if RUN_ONCE:
        one_pass()
    else:
        print("Бот запущен. Отправь ему /start в Telegram. Ctrl+C — остановить.")
        while True:
            one_pass()
            time.sleep(POLL_SECONDS)
