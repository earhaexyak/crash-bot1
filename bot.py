"""
bot.py - Telegram Crash Game bot (Aiogram 3.x), VIRTUAL CURRENCY ONLY.

- Every user starts with STARTING_BALANCE points (config.py).
- /bet <amount> starts a round; a message updates live with the rising
  multiplier and a "Cash Out" button. Cash out before the crash to win
  bet * multiplier points; otherwise the bet is lost.
- Admin-only commands (ADMIN_IDS in config.py / .env) let you add, set,
  or zero-out any user's balance by their Telegram user_id.

Run: python bot.py
"""

import asyncio
import logging
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
    MenuButtonWebApp,
)
from aiogram.exceptions import TelegramBadRequest

import config
import db
import game_logic

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)r}',
)
log = logging.getLogger("crash_bot")

bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# In-memory registry of rounds currently in flight: user_id -> RoundState
active_rounds: dict[int, "RoundState"] = {}
_round_counter = 0


class RoundState:
    __slots__ = ("user_id", "chat_id", "message_id", "bet", "server_seed",
                 "round_id", "crash_point", "start_ts", "cashed_out", "task")

    def __init__(self, user_id, chat_id, message_id, bet, round_id):
        self.user_id = user_id
        self.chat_id = chat_id
        self.message_id = message_id
        self.bet = bet
        self.round_id = round_id
        self.server_seed = game_logic.generate_server_seed()
        self.crash_point = game_logic.crash_point_from_seed(self.server_seed, round_id)
        self.start_ts = time.monotonic()
        self.cashed_out = False
        self.task = None


def cashout_keyboard(round_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💸 Cash Out", callback_data=f"cashout:{round_id}")
    ]])


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


# ---------------------------------------------------------------- commands

def webapp_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 افتح اللعبة", web_app=WebAppInfo(url=config.WEBAPP_URL))
    ]])


@dp.message(Command("start"))
async def cmd_start(message: Message):
    user = await db.get_or_create_user(message.from_user.id, message.from_user.username,
                                        config.STARTING_BALANCE)
    await message.answer(
        f"🚀 <b>Crash Game</b> — عملة وهمية للتسلية فقط، بدون فلوس حقيقية.\n\n"
        f"رصيدك: <b>{user['balance']:.2f} {config.CURRENCY_NAME}</b>\n\n"
        f"اضغط الزر تحت تفتح اللعبة كصفحة ويب داخل تيليجرام، أو استخدم:\n"
        f"/bet المبلغ — ابدأ جولة بالشات (مثال: <code>/bet 100</code>)\n"
        f"/balance — عرض رصيدك\n"
        f"/history — آخر 5 جولات",
        reply_markup=webapp_keyboard(),
    )


@dp.message(Command("play"))
async def cmd_play(message: Message):
    await message.answer("اضغط تفتح اللعبة 👇", reply_markup=webapp_keyboard())


@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    user = await db.get_or_create_user(message.from_user.id, message.from_user.username,
                                        config.STARTING_BALANCE)
    await message.answer(f"رصيدك الحالي: <b>{user['balance']:.2f} {config.CURRENCY_NAME}</b>")


@dp.message(Command("history"))
async def cmd_history(message: Message):
    stats = await db.user_stats(message.from_user.id)
    played = stats["rounds_played"] or 0
    wins = stats["wins"] or 0
    await message.answer(
        f"📊 الجولات: {played} | فوز: {wins} | خسارة: {played - wins}\n"
        f"إجمالي الرهانات: {stats['total_bet']:.2f} | إجمالي الأرباح: {stats['total_won']:.2f}"
    )


@dp.message(Command("bet"))
async def cmd_bet(message: Message, command: CommandObject):
    user_id = message.from_user.id

    if user_id in active_rounds:
        await message.answer("عندك جولة شغالة حالياً. سوّي Cash Out أول.")
        return

    if not command.args:
        await message.answer(f"استخدم: <code>/bet 100</code>\n(الحد الأدنى {config.MIN_BET}, الأعلى {config.MAX_BET})")
        return

    try:
        bet_amount = float(command.args.strip())
    except ValueError:
        await message.answer("المبلغ لازم يكون رقم صحيح. مثال: <code>/bet 100</code>")
        return

    if bet_amount < config.MIN_BET or bet_amount > config.MAX_BET:
        await message.answer(f"المبلغ لازم يكون بين {config.MIN_BET} و {config.MAX_BET}.")
        return

    user = await db.get_or_create_user(user_id, message.from_user.username, config.STARTING_BALANCE)
    if user["balance"] < bet_amount:
        await message.answer(f"رصيدك ما يكفي. رصيدك الحالي: {user['balance']:.2f} {config.CURRENCY_NAME}")
        return

    await db.adjust_balance(user_id, -bet_amount, "bet")

    global _round_counter
    _round_counter += 1
    round_id = _round_counter

    sent = await message.answer(
        f"🚀 <b>1.00x</b>\nرهانك: {bet_amount:.2f} {config.CURRENCY_NAME}",
        reply_markup=cashout_keyboard(round_id),
    )

    state = RoundState(user_id, sent.chat.id, sent.message_id, bet_amount, round_id)
    active_rounds[user_id] = state
    state.task = asyncio.create_task(run_round(state))


async def run_round(state: RoundState):
    """Ticks the multiplier upward until it hits the pre-computed crash point,
    editing the message roughly every 0.4s. Deliberately coarse-grained to
    respect Telegram's per-chat edit rate limits."""
    try:
        while True:
            elapsed_ms = (time.monotonic() - state.start_ts) * 1000
            current = round(pow(2.71828, game_logic.GROWTH_RATE * elapsed_ms), 2)

            if state.cashed_out:
                return

            if current >= state.crash_point:
                await bust(state)
                return

            try:
                await bot.edit_message_text(
                    chat_id=state.chat_id,
                    message_id=state.message_id,
                    text=f"🚀 <b>{current:.2f}x</b>\nرهانك: {state.bet:.2f} {config.CURRENCY_NAME}",
                    reply_markup=cashout_keyboard(state.round_id),
                )
            except TelegramBadRequest:
                pass  # message not modified / rate limited - safe to ignore

            await asyncio.sleep(0.4)
    except Exception:
        log.exception("run_round crashed for user %s", state.user_id)
        active_rounds.pop(state.user_id, None)


async def bust(state: RoundState):
    active_rounds.pop(state.user_id, None)
    await db.record_round(state.user_id, state.bet, state.crash_point, None, 0.0)
    try:
        await bot.edit_message_text(
            chat_id=state.chat_id,
            message_id=state.message_id,
            text=f"💥 <b>انفجر عند {state.crash_point:.2f}x</b>\nخسرت {state.bet:.2f} {config.CURRENCY_NAME}\n\n"
                 f"🔑 seed: <code>{state.server_seed[:16]}...</code>",
        )
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data.startswith("cashout:"))
async def cb_cashout(callback: CallbackQuery):
    round_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    state = active_rounds.get(user_id)

    if not state or state.round_id != round_id:
        await callback.answer("ما عندك جولة نشطة.", show_alert=True)
        return
    if state.cashed_out:
        await callback.answer("سويت Cash Out خلص.", show_alert=True)
        return

    elapsed_ms = (time.monotonic() - state.start_ts) * 1000
    current = round(pow(2.71828, game_logic.GROWTH_RATE * elapsed_ms), 2)

    if current >= state.crash_point:
        # crashed a split second before the tap landed - no payout
        await callback.answer("للأسف انفجر قبل ما توصل! 💥", show_alert=True)
        return

    state.cashed_out = True
    active_rounds.pop(user_id, None)
    payout = round(state.bet * current, 2)

    await db.adjust_balance(user_id, payout, "cashout")
    await db.record_round(user_id, state.bet, state.crash_point, current, payout)

    try:
        await bot.edit_message_text(
            chat_id=state.chat_id,
            message_id=state.message_id,
            text=f"✅ <b>سحبت عند {current:.2f}x</b>\nربحت {payout:.2f} {config.CURRENCY_NAME}",
        )
    except TelegramBadRequest:
        pass
    await callback.answer(f"ربحت {payout:.2f} {config.CURRENCY_NAME}! 🎉")


# ---------------------------------------------------------------- admin commands
# All three map directly to the "أضيف / أصفر / أضيف" request:
#   /addbalance <user_id> <amount>   -> credit points to a user by id
#   /setbalance <user_id> <amount>   -> force balance to an exact value
#   /resetbalance <user_id>          -> zero the balance out ("أصفر")

@dp.message(Command("addbalance"))
async def cmd_addbalance(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    parts = (command.args or "").split()
    if len(parts) != 2:
        await message.answer("استخدم: <code>/addbalance USER_ID AMOUNT</code>")
        return
    try:
        target_id, amount = int(parts[0]), float(parts[1])
    except ValueError:
        await message.answer("USER_ID لازم رقم صحيح و AMOUNT رقم.")
        return

    await db.get_or_create_user(target_id, None, 0)
    new_balance = await db.adjust_balance(target_id, amount, "admin_add")
    log.info('{"event":"admin_add","admin":%d,"target":%d,"amount":%f}',
             message.from_user.id, target_id, amount)
    await message.answer(f"تم. رصيد {target_id} الجديد: {new_balance:.2f} {config.CURRENCY_NAME}")


@dp.message(Command("setbalance"))
async def cmd_setbalance(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    parts = (command.args or "").split()
    if len(parts) != 2:
        await message.answer("استخدم: <code>/setbalance USER_ID AMOUNT</code>")
        return
    try:
        target_id, amount = int(parts[0]), float(parts[1])
    except ValueError:
        await message.answer("USER_ID لازم رقم صحيح و AMOUNT رقم.")
        return

    await db.set_balance(target_id, amount, "admin_set")
    log.info('{"event":"admin_set","admin":%d,"target":%d,"amount":%f}',
             message.from_user.id, target_id, amount)
    await message.answer(f"تم تثبيت رصيد {target_id} على {amount:.2f} {config.CURRENCY_NAME}")


@dp.message(Command("resetbalance"))
async def cmd_resetbalance(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    parts = (command.args or "").split()
    if len(parts) != 1:
        await message.answer("استخدم: <code>/resetbalance USER_ID</code>")
        return
    try:
        target_id = int(parts[0])
    except ValueError:
        await message.answer("USER_ID لازم رقم صحيح.")
        return

    await db.set_balance(target_id, 0.0, "admin_reset")
    log.info('{"event":"admin_reset","admin":%d,"target":%d}', message.from_user.id, target_id)
    await message.answer(f"تم تصفير رصيد {target_id}.")


@dp.message(Command("userinfo"))
async def cmd_userinfo(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    parts = (command.args or "").split()
    if len(parts) != 1:
        await message.answer("استخدم: <code>/userinfo USER_ID</code>")
        return
    try:
        target_id = int(parts[0])
    except ValueError:
        await message.answer("USER_ID لازم رقم صحيح.")
        return
    user = await db.get_user(target_id)
    if not user:
        await message.answer("ما لكيت هذا المستخدم.")
        return
    stats = await db.user_stats(target_id)
    await message.answer(
        f"👤 {target_id} (@{user['username'] or '-'})\n"
        f"الرصيد: {user['balance']:.2f}\n"
        f"جولات: {stats['rounds_played'] or 0} | فوز: {stats['wins'] or 0}"
    )


async def main():
    db.init_db()
    log.info("bot starting")
    await bot.delete_webhook(drop_pending_updates=True)

    # Persistent menu button (next to the message input, always visible -
    # not just a reply to /start). Same effect as BotFather's "Menu Button"
    # setting, done here so it self-configures on every deploy.
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="🚀 العب الآن", web_app=WebAppInfo(url=config.WEBAPP_URL))
    )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
