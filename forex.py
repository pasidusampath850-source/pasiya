AutoTrader (multi-symbol) with Telegram notifications.
Notes:
- Replace ADMINS with your Telegram numeric user id(s).
- Test on DEMO first.
- Keep MT5 and TELEGRAM credentials secret.
"""

import time
import threading
from datetime import datetime, timezone
import MetaTrader5 as mt5
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ========== CONFIG ==========
MT5_LOGIN = 265337487                # <-- set
MT5_PASSWORD = "Sampath123$#@"       # <-- set
MT5_SERVER = "Exness-MT5Real38"      # <-- set

TELEGRAM_TOKEN = "7786434709:AAE29u264oOFf9qH0oBSmjfTKQLSlUu_TUo"  # <-- set

# Replace with your correct Telegram numeric user id(s). Example: {6711430693}
ADMINS = {94701619816}  # <-- set your telegram id here (must be int)

SYMBOLS = ["XAUUSD", "BTCUSD", "EURUSD"]
MIN_LOT = 0.01
RISK_PERCENT = 0.0001         # can be a fraction (<1) e.g., 0.01 = 1% OR a percent value (>=1) e.g., 1 = 1%
STOP_LOSS_PIPS = {"XAUUSD": 50, "BTCUSD": 100, "EURUSD": 20}
TAKE_PROFIT_PIPS = {"XAUUSD": 100, "BTCUSD": 200, "EURUSD": 20}
CHECK_INTERVAL = 30
DAILY_PROFIT_TARGET = 500000.0
MAGIC = 20250918
ORDER_DEVIATION = 50
running = true
mode = None  # "safe" or "unlimited"
start_balance = None
lock = threading.Lock()


# ---------- Helpers & MT5 ----------
def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


def init_mt5():
    """
    Initialize and login MT5. Returns (ok: bool, message: str)
    """
    try:
        # Try to initialize with credentials (preferred), fallback to separate login
        initialized = mt5.initialize(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER)
        if not initialized:
            # fallback: shutdown then try initialize() then login()
            mt5.shutdown()
            if not mt5.initialize():
                return False, "MT5 initialize() failed"
            if not mt5.login(MT5_LOGIN, MT5_PASSWORD, MT5_SERVER):
                return False, "MT5 login failed"
        return True, "MT5 connected"
    except Exception as e:
        return False, f"MT5 init error: {e}"


def get_account_info_dict():
    acc = mt5.account_info()
    if not acc:
        return {}
    return {
        "login": getattr(acc, "login", None),
        "balance": float(getattr(acc, "balance", 0.0)),
        "equity": float(getattr(acc, "equity", 0.0)),
        "margin": float(getattr(acc, "margin", 0.0)),
        "free_margin": float(getattr(acc, "margin_free", 0.0)),
        "leverage": getattr(acc, "leverage", None),
        "currency": getattr(acc, "currency", "USD"),
    }


def format_wallet_snapshot():
    info = get_account_info_dict()
    if not info:
        return "Account info unavailable"
    return (
        f"Wallet Snapshot:\n"
        f"Login: {info['login']}\n"
        f"Balance: ${info['balance']:.2f}\n"
        f"Equity: ${info['equity']:.2f}\n"
        f"Margin: ${info['margin']:.2f}\n"
        f"Free Margin: ${info['free_margin']:.2f}\n"
        f"Leverage: {info['leverage']}\n"
    )


def get_today_profit():
    global start_balance
    acc = mt5.account_info()
    if not acc or start_balance is None:
        return 0.0
    try:
        return round(float(acc.balance) - float(start_balance), 2)
    except Exception:
        return 0.0


def ensure_symbol(symbol):
    si = mt5.symbol_info(symbol)
    if si is None:
        return False
    if not si.visible:
        mt5.symbol_select(symbol, True)
    return True


def compute_lot(symbol, stop_loss_pips, risk_percent):
    """
    Compute lot based on balance and risk.
    Accepts two styles for risk_percent:
      - fraction (0.01 = 1%) if < 1
      - percent (1 = 1%) if >= 1
    """
    try:
        balance = float(getattr(mt5.account_info(), "balance", 0.0))
    except Exception:
        balance = 0.0

    # normalize risk_percent to fraction
    if risk_percent <= 0:
        return MIN_LOT
    if risk_percent < 1:
        risk_fraction = risk_percent  # already fraction
    else:
        risk_fraction = risk_percent / 100.0

    risk_amount = balance * risk_fraction
    si = mt5.symbol_info(symbol)
    if si is None:
        return MIN_LOT

    # conservative pip value fallback (used only if we can't compute via tickvalue)
    # attempt to compute pip value per lot using tick value if available
    pip_value_per_lot = None
    try:
        # many brokers expose contract_size and tick_value/tick_size; try a fallback approach
        # but if not available, use sensible defaults:
        if hasattr(si, "trade_tick_value") and si.trade_tick_value:
            # trade_tick_value is per lot for 1 point? keep as fallback
            pip_value_per_lot = float(si.trade_tick_value)
    except Exception:
        pip_value_per_lot = None

    # sensible defaults if pip_value_per_lot unknown (these are conservative estimates)
    if pip_value_per_lot is None:
        if symbol == "EURUSD":
            pip_value_1lot = 10.0
        elif symbol == "XAUUSD":
            pip_value_1lot = 100.0
        elif symbol == "BTCUSD":
            pip_value_1lot = 1000.0
        else:
            pip_value_1lot = 10.0
    else:
        pip_value_1lot = pip_value_per_lot

    pip_value_0_01 = pip_value_1lot * 0.01  # value for 0.01 lot
    denom = stop_loss_pips * pip_value_0_01
    if denom <= 0 or risk_amount <= 0:
        return MIN_LOT
    lot = round(risk_amount / denom, 2)
    if lot < MIN_LOT:
        return MIN_LOT
    return lot


def place_market_order(symbol, side, lot, sl_pips=None, tp_pips=None):
    """
    Place market order. Returns (True/False, message)
    Takes care of pip scaling depending on symbol digits.
    """
    if not ensure_symbol(symbol):
        return False, "Symbol not available"

    tick = mt5.symbol_info_tick(symbol)
    si = mt5.symbol_info(symbol)
    if tick is None or si is None:
        return False, "No tick/symbol info"

    # determine price and pip/point scaling
    price = tick.ask if side == "buy" else tick.bid
    point = si.point
    # For brokers with 5 (or 3) digits, pip is 10 * point (common). For 4/2 digits pip ~= point
    pip_factor = 10 if si.digits in (3, 5) or si.digits >= 5 else 1

    sl = None
    tp = None
    if sl_pips is not None:
        if side == "buy":
            sl = price - sl_pips * point * pip_factor
        else:
            sl = price + sl_pips * point * pip_factor
    if tp_pips is not None:
        if side == "buy":
            tp = price + tp_pips * point * pip_factor
        else:
            tp = price - tp_pips * point * pip_factor

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lot),
        "type": mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": ORDER_DEVIATION,
        "magic": MAGIC,
        "comment": f"AutoTrade {side}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    try:
        res = mt5.order_send(request)
    except Exception as e:
        return False, f"Order_send exception: {e}"

    if res is None:
        return False, "Order_send returned None"
    # Some MT5 builds return complex object; use retcode if present
    retcode = getattr(res, "retcode", None)
    if retcode is not None and retcode == mt5.TRADE_RETCODE_DONE:
        return True, f"Order executed: {side} {symbol} lot={lot} price={price:.5f}"
    else:
        # try to extract comment or use string(res)
        err = getattr(res, "comment", None) or str(res)
        return False, f"Order failed: {err}"


# ---------- Simple signal ----------
def simple_signal(symbol):
    TF = mt5.TIMEFRAME_M5
    # use copy_rates_from_pos -> returns structured array with field names
    rates = mt5.copy_rates_from_pos(symbol, TF, 0, 100)
    if rates is None or len(rates) < 30:
        return None
    # extract closes using field name
    try:
        closes = [r['close'] for r in rates]
    except Exception:
        # fallback: maybe attribute access
        closes = [getattr(r, 'close', None) for r in rates]
        closes = [c for c in closes if c is not None]
    if len(closes) < 30:
        return None
    sma5 = sum(closes[-5:]) / 5
    sma20 = sum(closes[-20:]) / 20
    if sma5 > sma20:
        return "buy"
    elif sma5 < sma20:
        return "sell"
    return None


# ---------- Workers ----------
def trade_worker(mode_local, bot_instance):
    """
    Worker running in a background thread. Uses bot_instance to send messages
    (pass in application.bot).
    """
    admin_id = next(iter(ADMINS))
    try:
        bot_instance.send_message(chat_id=admin_id, text=f"{TAGLINE} ON\nMode: {mode_local}\n{format_wallet_snapshot()}")
    except Exception:
        pass

    global running, mode
    while running and mode == mode_local:
        for sym in SYMBOLS:
            if not running or mode != mode_local:
                break
            try:
                snapshot = format_wallet_snapshot()
                try:
                    bot_instance.send_message(chat_id=admin_id, text=f"Pre-Trade Snapshot for {sym}:\n{snapshot}\nSymbol: {sym}\nRisk%: {RISK_PERCENT}")
                except Exception:
                    pass

                sig = simple_signal(sym)
                if sig:
                    sl = STOP_LOSS_PIPS.get(sym, 20)
                    tp = TAKE_PROFIT_PIPS.get(sym, 20)
                    lot = compute_lot(sym, sl, RISK_PERCENT)
                    try:
                        bot_instance.send_message(chat_id=admin_id, text=f"Placing {sig.upper()} {sym} | Lot: {lot} | SL:{sl} pips | TP:{tp} pips")
                    except Exception:
                        pass

                    ok, msg = place_market_order(sym, sig, lot, sl_pips=sl, tp_pips=tp)
                    try:
                        bot_instance.send_message(chat_id=admin_id, text=f"{msg}")
                    except Exception:
                        pass

                    profit = get_today_profit()
                    try:
                        bot_instance.send_message(chat_id=admin_id, text=f"📊 Current Profit: ${profit:.2f}")
                    except Exception:
                        pass

                    if profit >= DAILY_PROFIT_TARGET:
                        try:
                            bot_instance.send_message(chat_id=admin_id, text=(
                                f"💰 DAILY TARGET REACHED: ${profit:.2f}\n"
                                "DAILY TARGET COMPLETE\n"
                                f"{TAGLINE}"
                            ))
                        except Exception:
                            pass
                        with lock:
                            running = False
                            mode = None
                        break
                else:
                    # optional no-signal note
                    try:
                        bot_instance.send_message(chat_id=admin_id, text=f"{sym}: No trade signal at this time.")
                    except Exception:
                        pass
                time.sleep(CHECK_INTERVAL)
            except Exception as e:
                try:
                    bot_instance.send_message(chat_id=admin_id, text=f"⚠ Error in worker for {sym}: {e}")
                except Exception:
                    pass
                time.sleep(5)

    try:
        bot_instance.send_message(chat_id=admin_id, text=f"{TAGLINE} STOPPED\nMode was: {mode_local}")
    except Exception:
        pass


# ---------- Telegram handlers ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    await update.message.reply_text("👋 Bot alive. Use /safe or /unlimited to start.")


async def cmd_safe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global running, mode, start_balance
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    with lock:
        if running:
            await update.message.reply_text("⚠ Bot already running.")
            return
        ok, msg = init_mt5()
        if not ok:
            await update.message.reply_text(f"MT5 connect failed: {msg}")
            return
        start_balance = float(getattr(mt5.account_info(), "balance", 0.0))
        running = True
        mode = "safe"
        admin_id = next(iter(ADMINS))
        # use context.application.bot (safe for async app)
        bot_instance = context.application.bot
        try:
            bot_instance.send_message(chat_id=admin_id, text=f"{TAGLINE} ON\nMode: SAFE\n{format_wallet_snapshot()}")
        except Exception:
            pass
        t = threading.Thread(target=trade_worker, args=("safe", bot_instance), daemon=True)
        t.start()
        await update.message.reply_text("Safe mode started.")


async def cmd_unlimited(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global running, mode, start_balance
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    with lock:
        if running:
            await update.message.reply_text("⚠ Bot already running.")
            return
        ok, msg = init_mt5()
        if not ok:
            await update.message.reply_text(f"MT5 connect failed: {msg}")
            return
        start_balance = float(getattr(mt5.account_info(), "balance", 0.0))
        running = True
        mode = "unlimited"
        admin_id = next(iter(ADMINS))
        bot_instance = context.application.bot
        try:
            bot_instance.send_message(chat_id=admin_id, text=f"{TAGLINE} ON\nMode: UNLIMITED\n{format_wallet_snapshot()}")
        except Exception:
            pass
        t = threading.Thread(target=trade_worker, args=("unlimited", bot_instance), daemon=True)
        t.start()
        await update.message.reply_text("Unlimited mode started.")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global running, mode
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    with lock:
        running = False
        old_mode = mode
        mode = None
        admin_id = next(iter(ADMINS))
        bot_instance = context.application.bot
        try:
            bot_instance.send_message(chat_id=admin_id, text=(
                f"{TAGLINE} STOPPED\nStopped by admin.\n{format_wallet_snapshot()}\nMode was: {old_mode}"
            ))
        except Exception:
            pass
    await update.message.reply_text("Bot stopped.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    profit = get_today_profit()
    await update.message.reply_text(
        f"Mode: {mode}\nRunning: {running}\nStartBalance: {start_balance}\nCurrentProfit: ${profit:.2f}"
    )


# ---------- Main ----------
def main():
    # attempt initial MT5 connect so account info can be used (optional)
    ok, msg = init_mt5()
    admin_id = next(iter(ADMINS)) if ADMINS else None
    if ok and admin_id:
        try:
            # note: we don't yet have application.bot until app is built; but we can print
            print("MT5 connected. Account:", format_wallet_snapshot())
        except Exception:
            pass
    else:
        print("MT5 init:", msg)

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("safe", cmd_safe))
    app.add_handler(CommandHandler("unlimited", cmd_unlimited))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))

    print("Bot polling (CTRL+C to exit)...")
    app.run_polling()


if _name_ == "_main_":
    main()
