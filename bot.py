import asyncio
import html
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import (InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup,
                      InputMediaPhoto, Update)
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

from poker import Table, card_str
from render import render_table

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["BOT_TOKEN"]
TURN_TIME = 45          # ثانیه برای هر نوبت
NEXT_HAND_DELAY = 7     # ثانیه تا دست بعدی
TABLES: dict[int, Table] = {}

STAGE_FA = {"preflop": "پری‌فلاپ", "flop": "فلاپ", "turn": "ترن",
            "river": "ریور", "showdown": "رو کردن کارت‌ها"}
HAND_FA = ["کارت بالا", "یک جفت", "دو جفت", "سه‌تایی", "استریت",
           "فلاش", "فول‌هاوس", "چهارتایی", "استریت فلاش"]

HELP = (
    "🃏 <b>ربات پوکر تگزاس هولدم</b>\n\n"
    "من رو به گروه اضافه کن و بنویس /poker تا میز ساخته بشه.\n"
    "همه با دکمه‌ها بازی می‌کنن، چیپ‌ها مجازی هستن.\n\n"
    "/poker — ساخت میز جدید\n"
    "/join — نشستن سر میز\n"
    "/leave — ترک میز\n"
    "/mode — تغییر بین حالت عکس و متن\n"
    "/stop — پایان بازی (سازنده یا ادمین)"
)


def esc(s):
    return html.escape(s or "")


def uname(u):
    return u.first_name or u.username or str(u.id)


# ---------- ساخت پیام‌ها ----------
def lobby_text(t):
    names = "\n".join(f"{i + 1}. {esc(p.name)}" for i, p in enumerate(t.players + t.queue))
    return (f"🃏 <b>میز پوکر</b>\nچیپ شروع: {t.start_chips} | بلایند: {t.SB}/{t.BB}\n\n"
            f"بازیکن‌ها:\n{names or '—'}\n\nحداقل ۲ نفر، حداکثر ۹ نفر.")


def lobby_kb():
    return Markup([[Btn("🪑 نشستن", callback_data="L:join"), Btn("🚪 رفتن", callback_data="L:leave")],
                   [Btn("▶️ شروع بازی", callback_data="L:start")]])


def caption(t):
    lines = [f"🃏 دست #{t.hand_no} — {STAGE_FA[t.stage]}", f"💰 پات: {t.pot()}"]
    if t.board:
        lines.append("میز: " + " ".join(card_str(c) for c in t.board))
    lines.append("")
    for i, p in enumerate(t.players):
        mark = "👉 " if i == t.to_act else ""
        tags = ""
        if i == t.dealer:
            tags += " 🔘"
        if p.folded:
            tags += " ❌فولد"
        elif p.allin:
            tags += " 🔥آل‌این"
        bet = f" | شرط: {p.bet}" if p.bet else ""
        lines.append(f"{mark}#{i + 1} {esc(p.name)} — {p.chips}{bet}{tags}")
    cur = t.current_player()
    if cur:
        need = t.call_amount(cur)
        lines.append(f"\n⏳ نوبت <b>{esc(cur.name)}</b> ({TURN_TIME} ثانیه)"
                     + (f" — برای کال {need}" if need else ""))
    return "\n".join(lines)


def action_kb(t):
    cur = t.current_player()
    if not cur:
        return None
    h = t.hand_no
    need = t.call_amount(cur)
    row1 = [Btn("❌ فولد", callback_data=f"A:{h}:fold:0"),
            Btn("✅ چک" if need == 0 else f"📞 کال {need}", callback_data=f"A:{h}:call:0")]
    labels = {"min": "⬆️ حداقل", "half": "½ پات", "pot": "پات", "allin": "🔥 آل‌این"}
    row2 = [Btn(f"{labels[k]} {v}" if k != "allin" else f"{labels[k]} ({v})",
                callback_data=f"A:{h}:raise:{v}") for k, v in t.raise_options()]
    rows = [row1]
    for i in range(0, len(row2), 2):
        rows.append(row2[i:i + 2])
    rows.append([Btn("🃏 کارت‌های من", callback_data=f"A:{h}:cards:0")])
    return Markup(rows)


# ---------- نمایش میز ----------
async def strip_old(ctx, t):
    if t.msg_id:
        try:
            await ctx.bot.edit_message_reply_markup(t.chat_id, t.msg_id, reply_markup=None)
        except Exception:
            pass
        t.msg_id = None


async def show(ctx, t, new=False):
    cap, kb = caption(t), action_kb(t)
    if new or not t.msg_id:
        await strip_old(ctx, t)
        if t.image_mode:
            img = await asyncio.to_thread(render_table, t)
            m = await ctx.bot.send_photo(t.chat_id, img, caption=cap, reply_markup=kb, parse_mode="HTML")
        else:
            m = await ctx.bot.send_message(t.chat_id, cap, reply_markup=kb, parse_mode="HTML")
        t.msg_id = m.message_id
        return
    try:
        if t.image_mode:
            img = await asyncio.to_thread(render_table, t)
            await ctx.bot.edit_message_media(
                chat_id=t.chat_id, message_id=t.msg_id,
                media=InputMediaPhoto(img, caption=cap, parse_mode="HTML"), reply_markup=kb)
        else:
            await ctx.bot.edit_message_text(cap, t.chat_id, t.msg_id, reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            t.msg_id = None
            await show(ctx, t, new=True)


def cancel_timer(t):
    if t.job:
        t.job.schedule_removal()
        t.job = None


def schedule_turn(ctx, t):
    cancel_timer(t)
    cur = t.current_player()
    if cur:
        t.job = ctx.job_queue.run_once(on_timeout, TURN_TIME, data=(t.chat_id, t.hand_no, cur.uid))


async def after_action(ctx, t, prev_stage):
    if t.state == "hand":
        await show(ctx, t, new=(t.stage != prev_stage))
        schedule_turn(ctx, t)
    else:
        cancel_timer(t)
        await finish_hand(ctx, t)


async def begin_hand(ctx, t):
    if not t.start_hand():
        await ctx.bot.send_message(t.chat_id, "برای شروع حداقل ۲ بازیکن لازمه.")
        return
    await ctx.bot.send_message(
        t.chat_id, f"🎴 دست #{t.hand_no} شروع شد! برای دیدن کارت‌هات دکمه «🃏 کارت‌های من» رو بزن.")
    await after_action(ctx, t, None)


async def finish_hand(ctx, t):
    res = t.result
    await strip_old(ctx, t)
    lines = [f"🏁 <b>پایان دست #{t.hand_no}</b>"]
    if t.board:
        lines.append("میز: " + " ".join(card_str(c) for c in t.board))
    if res["reveal"]:
        lines.append("")
        for p, hole, rk in res["reveal"]:
            i = t.players.index(p) + 1
            lines.append(f"#{i} {esc(p.name)}: {' '.join(card_str(c) for c in hole)} — {HAND_FA[rk]}")
    lines.append("")
    for p, amt in res["won"]:
        lines.append(f"🏆 {esc(p.name)} برنده‌ی {amt} چیپ شد" + (" (بقیه فولد کردن)" if res["fold"] else ""))
    text = "\n".join(lines)
    if t.image_mode and not res["fold"]:
        img = await asyncio.to_thread(render_table, t, True)
        await ctx.bot.send_photo(t.chat_id, img, caption=text, parse_mode="HTML")
    else:
        await ctx.bot.send_message(t.chat_id, text, parse_mode="HTML")

    busted = [p for p in t.players if p.chips == 0]
    for p in busted:
        await ctx.bot.send_message(t.chat_id, f"💀 {esc(p.name)} همه‌ی چیپ‌هاش رو باخت و از میز رفت.", parse_mode="HTML")
    alive = [p for p in t.players if p.chips > 0 and not p.leaving] + t.queue
    if len(alive) >= 2:
        ctx.job_queue.run_once(next_hand_job, NEXT_HAND_DELAY, data=(t.chat_id, t.hand_no))
    else:
        t.players = [p for p in t.players if p.chips > 0 and not p.leaving]
        t.state = "lobby"
        if t.players:
            await ctx.bot.send_message(
                t.chat_id, f"🎉 بازی تموم شد! برنده: <b>{esc(t.players[0].name)}</b>\nبرای بازی جدید /poker", parse_mode="HTML")
        TABLES.pop(t.chat_id, None)


async def next_hand_job(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id, hand = ctx.job.data
    t = TABLES.get(chat_id)
    if not t:
        return
    async with t.lock:
        if t.state == "between" and t.hand_no == hand:
            await begin_hand(ctx, t)


async def on_timeout(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id, hand, uid = ctx.job.data
    t = TABLES.get(chat_id)
    if not t:
        return
    async with t.lock:
        if t.hand_no != hand or not t.can_act(uid):
            return
        p = t.current_player()
        prev = t.stage
        if t.call_amount(p) == 0:
            t.call()
            note = f"⏰ وقت {esc(p.name)} تموم شد، چک شد."
        else:
            t.fold()
            note = f"⏰ وقت {esc(p.name)} تموم شد، فولد شد."
        await ctx.bot.send_message(chat_id, note, parse_mode="HTML")
        await after_action(ctx, t, prev)


# ---------- دستورها ----------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode="HTML")


async def cmd_poker(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("پوکر چندنفره‌ست؛ من رو به یه گروه اضافه کن و اونجا /poker بزن.")
        return
    old = TABLES.get(chat.id)
    if old and old.state != "lobby":
        await update.message.reply_text("یه بازی در جریانه. برای پایان‌دادنش /stop")
        return
    t = Table(chat.id, update.effective_user.id)
    TABLES[chat.id] = t
    t.add_player(update.effective_user.id, uname(update.effective_user))
    m = await update.message.reply_text(lobby_text(t), reply_markup=lobby_kb(), parse_mode="HTML")
    t.lobby_msg_id = m.message_id


async def cmd_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = TABLES.get(update.effective_chat.id)
    if not t:
        await update.message.reply_text("میزی نیست. /poker")
        return
    r = t.add_player(update.effective_user.id, uname(update.effective_user))
    msg = {"ok": "نشستی ✅", "queued": "از دست بعد وارد بازی می‌شی ✅",
           "exists": "تو قبلاً سر میز هستی.", "full": "میز پره."}[r]
    await update.message.reply_text(msg)


async def cmd_leave(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = TABLES.get(update.effective_chat.id)
    if not t:
        return
    async with t.lock:
        prev = t.stage
        was_hand = t.state == "hand"
        if t.leave(update.effective_user.id):
            await update.message.reply_text("از میز رفتی 👋")
            if was_hand:
                await after_action(ctx, t, prev)


async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = TABLES.get(update.effective_chat.id)
    if not t:
        await update.message.reply_text("میزی نیست. /poker")
        return
    t.image_mode = not t.image_mode
    t.msg_id = None
    await update.message.reply_text("حالت: 🖼 عکس" if t.image_mode else "حالت: 📝 متن")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    t = TABLES.get(chat.id)
    if not t:
        return
    uid = update.effective_user.id
    member = await chat.get_member(uid)
    if uid != t.owner_id and member.status not in ("administrator", "creator"):
        await update.message.reply_text("فقط سازنده‌ی میز یا ادمین می‌تونه بازی رو تموم کنه.")
        return
    cancel_timer(t)
    await strip_old(ctx, t)
    TABLES.pop(chat.id, None)
    await update.message.reply_text("🛑 بازی تموم شد.")


# ---------- دکمه‌ها ----------
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat_id
    t = TABLES.get(chat_id)
    if not t:
        await q.answer("بازی‌ای در جریان نیست. /poker", show_alert=True)
        return
    parts = q.data.split(":")
    user = q.from_user
    if parts[0] == "L":
        await lobby_button(update, ctx, t, parts[1])
        return

    hand, act, amt = int(parts[1]), parts[2], int(parts[3])
    if act == "cards":
        p = t.find(user.id)
        if not p or not p.hole:
            await q.answer("تو توی این دست نیستی.", show_alert=True)
            return
        board = " ".join(card_str(c) for c in t.board) or "—"
        await q.answer(f"کارت‌های تو: {' '.join(card_str(c) for c in p.hole)}\nمیز: {board}", show_alert=True)
        return

    async with t.lock:
        if hand != t.hand_no or not t.can_act(user.id):
            await q.answer("الان نوبت تو نیست.")
            return
        prev = t.stage
        cancel_timer(t)
        if act == "fold":
            t.fold()
        elif act == "call":
            t.call()
        elif act == "raise":
            t.raise_to(amt)
        await q.answer()
        await after_action(ctx, t, prev)


async def lobby_button(update, ctx, t, act):
    q = update.callback_query
    user = q.from_user
    if act == "join":
        r = t.add_player(user.id, uname(user))
        await q.answer({"ok": "نشستی ✅", "queued": "دست بعد وارد می‌شی ✅",
                        "exists": "تو قبلاً نشستی.", "full": "میز پره."}[r])
    elif act == "leave":
        async with t.lock:
            if t.state == "lobby":
                t.leave(user.id)
        await q.answer("رفتی 👋")
    elif act == "start":
        async with t.lock:
            if t.state != "lobby":
                await q.answer("بازی شروع شده.")
                return
            if not t.find(user.id):
                await q.answer("اول باید بنشینی.", show_alert=True)
                return
            if len(t.players) < 2:
                await q.answer("حداقل ۲ بازیکن لازمه.", show_alert=True)
                return
            await q.answer()
            try:
                await q.message.edit_reply_markup(None)
            except Exception:
                pass
            await begin_hand(ctx, t)
        return
    try:
        await q.message.edit_text(lobby_text(t), reply_markup=lobby_kb(), parse_mode="HTML")
    except BadRequest:
        pass


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    logging.exception("خطا", exc_info=ctx.error)


# ---------- سرور سلامت (برای هاست‌های رایگانی که پورت می‌خوان) ----------
def start_health_server():
    port = os.environ.get("PORT")
    if not port:
        return

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    threading.Thread(target=HTTPServer(("0.0.0.0", int(port)), H).serve_forever, daemon=True).start()


def main():
    start_health_server()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("poker", cmd_poker))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("leave", cmd_leave))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
