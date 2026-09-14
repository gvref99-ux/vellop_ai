import asyncio
import logging
import sqlite3
import datetime
from io import BytesIO
from PIL import Image
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
import google.generativeai as genai

# ================= НАСТРОЙКИ =================
BOT_TOKEN = "TG_TOKEN"
GEMINI_API_KEY = "GEMINI_TOKEN"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
genai.configure(api_key=GEMINI_API_KEY)
logging.basicConfig(level=logging.INFO)


class BotStates(StatesGroup):
    waiting_for_question = State()
    waiting_for_promo = State()
    waiting_for_support = State()
    waiting_for_receipt_sub = State()
    waiting_for_receipt_req = State()
    waiting_for_broadcast = State()  # Стейт для объявления админов


# ================= РАБОТА С ФАЙЛАМИ =================
def read_txt(filename):
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        return []


def get_admins(): return [int(x) for x in read_txt('admins.txt')]


def get_channels():
    return [{'name': p[0].strip(), 'id': p[1].strip(), 'link': p[2].strip()}
            for p in (line.split('|') for line in read_txt('channels.txt')) if len(p) >= 3]


def get_subs():
    return [{'name': p[0].strip(), 'id': int(p[1].strip()), 'days': int(p[2].strip()),
             'link': p[3].strip(), 'price': p[4].strip(), 'limit': int(p[5].strip())}
            for p in (line.split('|') for line in read_txt('subscriptions.txt')) if len(p) >= 6]


def get_sub_descs():
    return {int(p[0].strip()): p[1].strip().replace('\\n', '\n') for p in
            (line.split('|') for line in read_txt('sub_descriptions.txt')) if len(p) >= 2}


def get_sub_models():
    return {int(p[0].strip()): p[1].strip().split() for p in (line.split('|') for line in read_txt('sub_models.txt')) if
            len(p) >= 2}


def get_models_info():
    return {p[0].strip(): {'name': p[1].strip(), 'type': p[2].strip()}
            for p in (line.split('|') for line in read_txt('models_info.txt')) if len(p) >= 3}


def get_req_packs():
    return [{'name': p[0].strip(), 'amount': int(p[1].strip()), 'price': p[2].strip(), 'link': p[3].strip()}
            for p in (line.split('|') for line in read_txt('buy_requests.txt')) if len(p) >= 4]


def get_promocodes():
    return {p[0].strip(): {'type': p[1].strip(), 'value': int(p[2].strip())}
            for p in (line.split('|') for line in read_txt('promocodes.txt')) if len(p) >= 3}


# ================= БАЗА ДАННЫХ =================
def init_db():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, requests_left INTEGER, last_reset TEXT, 
                  sub_id INTEGER, sub_exp TEXT, referrer_id INTEGER, extra_requests INTEGER, current_model TEXT)''')
    conn.commit()
    conn.close()


def get_user(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    user = c.fetchone()
    conn.close()
    return user


def get_all_users():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    users = [row[0] for row in c.fetchall()]
    conn.close()
    return users


def add_user(user_id, referrer_id=None):
    if not get_user(user_id):
        conn = sqlite3.connect('users.db')
        today = datetime.date.today().isoformat()
        conn.execute("INSERT INTO users VALUES (?, 15, ?, 0, '2000-01-01', ?, 0, 'gemini-3.5-flash')",
                     (user_id, today, referrer_id))
        if referrer_id:
            conn.execute("UPDATE users SET extra_requests = extra_requests + 10 WHERE user_id=?", (referrer_id,))
        conn.commit()
        conn.close()


def update_requests(user_id):
    user = get_user(user_id)
    if not user: return 0, 0

    today = datetime.date.today()
    last_reset = datetime.date.fromisoformat(user[2])
    sub_exp = datetime.date.fromisoformat(user[4])
    sub_id = user[3]

    if sub_id != 0 and today > sub_exp:
        sub_id = 0
        conn = sqlite3.connect('users.db')
        conn.execute("UPDATE users SET sub_id=0, current_model='gemini-3.5-flash' WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()

    if last_reset != today:
        subs = get_subs()
        base_limit = 15
        for s in subs:
            if s['id'] == sub_id:
                base_limit = s['limit']
                break

        conn = sqlite3.connect('users.db')
        conn.execute("UPDATE users SET requests_left=?, last_reset=? WHERE user_id=?",
                     (base_limit, today.isoformat(), user_id))
        conn.commit()
        conn.close()
        return base_limit, user[6]

    return user[1], user[6]


def use_request(user_id):
    if user_id in get_admins(): return True

    req, extra = update_requests(user_id)
    if req >= 999999 or extra >= 999999:  # Бесконечные запросы
        return True

    conn = sqlite3.connect('users.db')
    if req > 0:
        conn.execute("UPDATE users SET requests_left = requests_left - 1 WHERE user_id=?", (user_id,))
        conn.commit()
        return True
    elif extra > 0:
        conn.execute("UPDATE users SET extra_requests = extra_requests - 1 WHERE user_id=?", (user_id,))
        conn.commit()
        return True
    return False


# ================= ПРОВЕРКА ПОДПИСОК =================
async def check_sub(user_id):
    if user_id in get_admins(): return []
    channels = get_channels()
    not_subbed = []

    for channel in channels:
        try:
            member = await bot.get_chat_member(chat_id=channel['id'], user_id=user_id)
            if member.status in ['left', 'kicked', 'banned']:
                not_subbed.append(channel)
        except Exception as e:
            logging.error(f"Ошибка при проверке подписки на {channel['id']}: {e}")
            if "user not found" in str(e).lower() or "chat not found" in str(e).lower():
                not_subbed.append(channel)
    return not_subbed


def get_sub_keyboard(not_subbed_channels):
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for ch in not_subbed_channels:
        kb.inline_keyboard.append([InlineKeyboardButton(text=ch['name'], url=ch['link'])])
    kb.inline_keyboard.append([InlineKeyboardButton(text="🔄 Проверить подписки", callback_data="check_channels")])
    return kb


# ================= КЛАВИАТУРЫ ГЛАВНОГО МЕНЮ =================
def get_main_menu(user_id):
    kb = [
        [KeyboardButton(text="💬 Задать вопрос"), KeyboardButton(text="🎛 Выбрать модель")],
        [KeyboardButton(text="💎 Купить подписку"), KeyboardButton(text="🔋 Купить запросы")],
        [KeyboardButton(text="📊 Мой профиль"), KeyboardButton(text="🔗 Реф. ссылка")],
        [KeyboardButton(text="🎁 Промокод"), KeyboardButton(text="🆘 Поддержка")]
    ]
    if user_id in get_admins():
        kb.append([KeyboardButton(text="📢 Выложить объявление")])  # Кнопка только для админов

    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


# ================= ХЭНДЛЕРЫ =================
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    args = message.text.split()
    referrer = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    add_user(message.from_user.id, referrer)

    not_subbed = await check_sub(message.from_user.id)
    if not_subbed:
        return await message.answer("🛑 Для использования бота подпишитесь на наши каналы:",
                                    reply_markup=get_sub_keyboard(not_subbed))

    await message.answer("✅ Добро пожаловать! Выберите действие в меню ниже.",
                         reply_markup=get_main_menu(message.from_user.id))


@dp.callback_query(F.data == "check_channels")
async def check_channels_cb(callback: types.CallbackQuery):
    not_subbed = await check_sub(callback.from_user.id)
    if not_subbed:
        await callback.answer("❌ Вы подписались не на все каналы!", show_alert=True)
    else:
        await callback.message.delete()
        await callback.message.answer(
            "✅ Спасибо за подписку! Добро пожаловать. Выберите действие в меню ниже.",
            reply_markup=get_main_menu(callback.from_user.id)
        )


# --- ПРОФИЛЬ ---
@dp.message(F.text == "📊 Мой профиль")
async def check_profile(message: types.Message):
    user = get_user(message.from_user.id)
    req, extra = update_requests(message.from_user.id)

    models = get_models_info()
    current_model_name = models.get(user[7], {}).get('name', user[7])

    is_admin = message.from_user.id in get_admins()

    req_display = str(req) if req < 999999 else "♾ Безлимитно"
    extra_display = str(extra) if extra < 999999 else "♾ Безлимитно"

    sub_text = "Бесплатная"
    if is_admin:
        sub_text = "👑 АДМИН (Всё доступно)"
        req_display = "♾ Безлимитно"
        extra_display = "♾ Безлимитно"
    elif user[3] != 0:
        sub_name = next((s['name'] for s in get_subs() if s['id'] == user[3]), f"ID {user[3]}")
        exp_date = user[4]
        sub_text = f"«{sub_name}»" if "9999" in exp_date else f"«{sub_name}» (до {exp_date})"

    text = (f"👤 **Ваш профиль:**\n\n"
            f"👑 Подписка: {sub_text}\n"
            f"🎛 Модель: {current_model_name}\n\n"
            f"🔋 Запросов на сегодня: {req_display}\n"
            f"🎁 Доп. запросов: {extra_display}")
    await message.answer(text, parse_mode="Markdown")


# --- ВЫБОР МОДЕЛИ (2 в ряд) ---
@dp.message(F.text == "🎛 Выбрать модель")
async def select_model_cmd(message: types.Message):
    user = get_user(message.from_user.id)

    if message.from_user.id in get_admins():
        allowed_models = list(get_models_info().keys())
    else:
        sub_models = get_sub_models()
        allowed_models = sub_models.get(user[3], ['gemini-3.5-flash'])

    models_info = get_models_info()
    kb = InlineKeyboardMarkup(inline_keyboard=[])

    row = []
    for m_code in allowed_models:
        if m_code in models_info:
            prefix = "✅ " if user[7] == m_code else ""
            row.append(
                InlineKeyboardButton(text=f"{prefix}{models_info[m_code]['name']}", callback_data=f"setmod_{m_code}"))

            if len(row) == 2:
                kb.inline_keyboard.append(row)
                row = []
    if row:
        kb.inline_keyboard.append(row)

    await message.answer("Выбери нейросеть для работы:\n*(Чем круче подписка - тем больше здесь ИИ)*", reply_markup=kb,
                         parse_mode="Markdown")


@dp.callback_query(F.data.startswith("setmod_"))
async def set_model_cb(callback: types.CallbackQuery):
    model_code = callback.data.replace('setmod_', '')
    conn = sqlite3.connect('users.db')
    conn.execute("UPDATE users SET current_model=? WHERE user_id=?", (model_code, callback.from_user.id))
    conn.commit()
    conn.close()

    model_name = get_models_info().get(model_code, {}).get('name', model_code)
    await callback.message.edit_text(f"✅ Модель успешно изменена на **{model_name}**!", parse_mode="Markdown")


# --- ЗАДАТЬ ВОПРОС (Теперь только ТЕКСТ) ---
@dp.message(F.text == "💬 Задать вопрос")
async def ask_q(message: types.Message, state: FSMContext):
    not_subbed = await check_sub(message.from_user.id)
    if not_subbed:
        return await message.answer("🛑 Сначала подпишитесь на каналы!", reply_markup=get_sub_keyboard(not_subbed))
    await state.set_state(BotStates.waiting_for_question)
    await message.answer(
        "Отправьте текст или фото (нейросеть опишет или проанализирует его).\n*(Напишите 'отмена' для выхода)*")


@dp.message(BotStates.waiting_for_question)
async def process_question(message: types.Message, state: FSMContext):
    if message.text and message.text.lower() == 'отмена':
        await state.clear()
        return await message.answer("Действие отменено.", reply_markup=get_main_menu(message.from_user.id))

    if not use_request(message.from_user.id):
        await state.clear()
        return await message.answer("❌ У вас закончились запросы! Купите тариф или подписку.")

    wait_msg = await message.answer("⏳ Нейросеть генерирует ответ...")
    user = get_user(message.from_user.id)
    current_model = user[7]
    model_info = get_models_info().get(current_model, {})

    try:
        clean_model_name = current_model.replace("models/", "")
        ai_model = genai.GenerativeModel(clean_model_name)

        prompt = message.text
        response = None

        try:
            if message.photo:
                photo = message.photo[-1]
                file_info = await bot.get_file(photo.file_id)
                downloaded_file = await bot.download_file(file_info.file_path)
                img = Image.open(downloaded_file)
                prompt = message.caption if message.caption else "Опиши подробно, что находится на этом фото."
                response = ai_model.generate_content([prompt, img])
            else:
                response = ai_model.generate_content(prompt)

        except Exception as e:
            if "404" in str(e) or "not found" in str(e).lower() or "unsupported" in str(e).lower():
                await wait_msg.edit_text(
                    f"⚠️ Модель **{model_info['name']}** сейчас перегружена.\n🔄 *Использую резервную 1.5 Pro...*",
                    parse_mode="Markdown")
                fallback_model = genai.GenerativeModel('gemini-1.5-pro')

                if message.photo:
                    response = fallback_model.generate_content([prompt, img])
                else:
                    response = fallback_model.generate_content(prompt)
            else:
                raise e

        if response:
            await wait_msg.edit_text(response.text)

    except Exception as e:
        await wait_msg.edit_text("❌ Произошла ошибка на серверах нейросети. Попробуйте выбрать другую модель.")
        logging.error(f"Ошибка ИИ: {e}")

    await state.clear()


# --- АДМИН: РАССЫЛКА (ОБЪЯВЛЕНИЯ) ---
@dp.message(F.text == "📢 Выложить объявление")
async def broadcast_btn(message: types.Message, state: FSMContext):
    if message.from_user.id not in get_admins(): return
    await state.set_state(BotStates.waiting_for_broadcast)
    await message.answer(
        "Напишите текст объявления (можно прикрепить фото/видео).\nЭто сообщение будет отправлено ВСЕМ пользователям бота.\n\nДля отмены напишите 'отмена'.")


@dp.message(BotStates.waiting_for_broadcast)
async def process_broadcast(message: types.Message, state: FSMContext):
    if message.text and message.text.lower() == 'отмена':
        await state.clear()
        return await message.answer("Рассылка отменена.", reply_markup=get_main_menu(message.from_user.id))

    await state.clear()
    wait_msg = await message.answer("⏳ Начинаю рассылку. Это может занять время...")

    users = get_all_users()
    success, fail = 0, 0

    for user_id in users:
        try:
            await message.copy_to(user_id)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1

    await wait_msg.edit_text(f"✅ **Рассылка завершена!**\n\nУспешно отправлено: {success}\nЗаблокировали бота: {fail}",
                             parse_mode="Markdown")


# --- ПОДДЕРЖКА ---
@dp.message(F.text == "🆘 Поддержка")
async def support_btn(message: types.Message, state: FSMContext):
    await state.set_state(BotStates.waiting_for_support)
    await message.answer("Опишите вашу проблему подробно (можно с фото).\nДля отмены напишите 'отмена'.")


@dp.message(BotStates.waiting_for_support)
async def process_support(message: types.Message, state: FSMContext):
    if message.text and message.text.lower() == 'отмена':
        await state.clear()
        return await message.answer("Отменено.")

    for admin in get_admins():
        try:
            await bot.send_message(admin,
                                   f"🆘 **Новое обращение от @{message.from_user.username or 'Без юзернейма'} (ID: {message.from_user.id})**",
                                   parse_mode="Markdown")
            await message.copy_to(admin)
        except:
            pass

    await message.answer("✅ Ваше обращение отправлено администрации.")
    await state.clear()


# --- ПОКУПКА ПОДПИСОК ---
@dp.message(F.text == "💎 Купить подписку")
async def buy_sub(message: types.Message):
    payments = get_subs()
    if not payments: return await message.answer("Подписки пока недоступны.")

    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for p in payments:
        kb.inline_keyboard.append(
            [InlineKeyboardButton(text=f"{p['name']} - {p['price']} руб.", callback_data=f"buysub_{p['id']}")])
    await message.answer("Выберите подписку:", reply_markup=kb)


@dp.callback_query(F.data.startswith("buysub_"))
async def process_buysub(callback: types.CallbackQuery):
    sub_id = int(callback.data.split('_')[1])
    payment = next((s for s in get_subs() if s['id'] == sub_id), None)
    desc = get_sub_descs().get(sub_id, "Нет описания.")

    text = (f"💳 **Тариф:** {payment['name']}\n\n"
            f"📄 **Что дает:**\n{desc}\n\n"
            f"💰 **К оплате:** {payment['price']} руб.\n"
            f"🔗 **Ссылка СБП:** {payment['link']}\n\n"
            f"❗️ **ВНИМАНИЕ:** Перейдите по ссылке, оплатите, затем нажмите «✅ Я оплатил» и **отправьте чек**!")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paids_sub_{sub_id}")]])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("paids_sub_"))
async def process_paid_sub(callback: types.CallbackQuery, state: FSMContext):
    sub_id = int(callback.data.split('_')[2])
    await state.update_data(purchase_id=sub_id)
    await state.set_state(BotStates.waiting_for_receipt_sub)
    await callback.message.edit_text(
        "📸 **Отправьте скриншот чека об оплате прямо сейчас!**\nБез чека услуга не выдается.", parse_mode="Markdown")


@dp.message(BotStates.waiting_for_receipt_sub, F.photo)
async def process_receipt_sub(message: types.Message, state: FSMContext):
    data = await state.get_data()
    sub_id = data['purchase_id']
    payment = next((s for s in get_subs() if s['id'] == sub_id), None)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить",
                              callback_data=f"apprsub_{message.from_user.id}_{sub_id}_{payment['days']}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decl_{message.from_user.id}")]
    ])

    for admin in get_admins():
        try:
            await bot.send_photo(admin, message.photo[-1].file_id,
                                 caption=f"💸 **ОПЛАТА ПОДПИСКИ**\nПользователь: @{message.from_user.username} (ID: {message.from_user.id})\nТовар: {payment['name']}\nСумма: {payment['price']} руб.",
                                 reply_markup=kb, parse_mode="Markdown")
        except:
            pass

    await message.answer("⏳ Чек отправлен. Ожидайте выдачи!")
    await state.clear()


# --- ПОКУПКА ЗАПРОСОВ ---
@dp.message(F.text == "🔋 Купить запросы")
async def buy_req(message: types.Message):
    packs = get_req_packs()
    if not packs: return await message.answer("Пакеты пока недоступны.")

    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for i, p in enumerate(packs):
        amount_txt = "♾ Безлимит" if p['amount'] >= 999999 else f"{p['amount']} шт"
        kb.inline_keyboard.append(
            [InlineKeyboardButton(text=f"{p['name']} ({amount_txt}) - {p['price']} руб.", callback_data=f"buyreq_{i}")])
    await message.answer("Выберите пакет запросов:", reply_markup=kb)


@dp.callback_query(F.data.startswith("buyreq_"))
async def process_buyreq(callback: types.CallbackQuery):
    idx = int(callback.data.split('_')[1])
    pack = get_req_packs()[idx]

    amount_txt = "♾ БЕЗЛИМИТНО" if pack['amount'] >= 999999 else f"{pack['amount']} шт."

    text = (f"💳 **Оплата пакета:** {pack['name']}\n"
            f"🔋 **Запросов:** {amount_txt}\n"
            f"💰 **К оплате:** {pack['price']} руб.\n"
            f"🔗 **Ссылка СБП:** {pack['link']}\n\n"
            f"❗️ **ОБЯЗАТЕЛЬНО:** Нажмите «✅ Я оплатил» и **отправьте чек**!")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paids_req_{idx}")]])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("paids_req_"))
async def process_paid_req(callback: types.CallbackQuery, state: FSMContext):
    idx = int(callback.data.split('_')[2])
    await state.update_data(pack_idx=idx)
    await state.set_state(BotStates.waiting_for_receipt_req)
    await callback.message.edit_text("📸 **Отправьте скриншот чека.**", parse_mode="Markdown")


@dp.message(BotStates.waiting_for_receipt_req, F.photo)
async def process_receipt_req(message: types.Message, state: FSMContext):
    data = await state.get_data()
    pack = get_req_packs()[data['pack_idx']]
    amount_txt = "БЕЗЛИМИТ" if pack['amount'] >= 999999 else f"{pack['amount']} шт"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Выдать", callback_data=f"apprreq_{message.from_user.id}_{pack['amount']}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decl_{message.from_user.id}")]
    ])

    for admin in get_admins():
        try:
            await bot.send_photo(admin, message.photo[-1].file_id,
                                 caption=f"🔋 **ОПЛАТА ЗАПРОСОВ**\nЮзер: @{message.from_user.username} ({message.from_user.id})\nПакет: {pack['name']} ({amount_txt})\nСумма: {pack['price']} руб.",
                                 reply_markup=kb, parse_mode="Markdown")
        except:
            pass
    await message.answer("⏳ Чек отправлен администраторам.")
    await state.clear()


# --- АДМИНСКИЕ КНОПКИ (ВЫДАЧА) ---
@dp.callback_query(F.data.startswith("apprsub_"))
async def admin_approve_sub(callback: types.CallbackQuery):
    if callback.from_user.id not in get_admins(): return
    _, user_id, sub_id, days = callback.data.split('_')
    sub_id = int(sub_id)

    exp_date = "2999-01-01" if int(days) >= 9999 else (
            datetime.date.today() + datetime.timedelta(days=int(days))).isoformat()

    sub_limit = next((s['limit'] for s in get_subs() if s['id'] == sub_id), 15)

    conn = sqlite3.connect('users.db')
    conn.execute("UPDATE users SET sub_id=?, sub_exp=?, requests_left=?, last_reset=? WHERE user_id=?",
                 (sub_id, exp_date, sub_limit, datetime.date.today().isoformat(), int(user_id)))
    conn.commit()
    conn.close()

    sub_name = next((s['name'] for s in get_subs() if s['id'] == sub_id), f"ID {sub_id}")
    await callback.message.edit_caption(caption=callback.message.caption + "\n\n✅ **ВЫДАНО!**")
    try:
        await bot.send_message(int(user_id),
                               f"🎉 Ваша подписка **«{sub_name}»** активирована!\nВам начислено {sub_limit} запросов на сегодня.\nПерейдите в меню '🎛 Выбрать модель'.",
                               parse_mode="Markdown")
    except:
        pass


@dp.callback_query(F.data.startswith("apprreq_"))
async def admin_approve_req(callback: types.CallbackQuery):
    if callback.from_user.id not in get_admins(): return
    _, user_id, amount = callback.data.split('_')

    conn = sqlite3.connect('users.db')
    conn.execute("UPDATE users SET extra_requests = extra_requests + ? WHERE user_id=?", (int(amount), int(user_id)))
    conn.commit()
    conn.close()

    await callback.message.edit_caption(caption=callback.message.caption + "\n\n✅ **ВЫДАНО!**")
    amount_txt = "БЕЗЛИМИТНОЕ КОЛИЧЕСТВО" if int(amount) >= 999999 else amount
    try:
        await bot.send_message(int(user_id), f"🎉 Вам успешно начислено {amount_txt} запросов!")
    except:
        pass


@dp.callback_query(F.data.startswith("decl_"))
async def admin_decline(callback: types.CallbackQuery):
    if callback.from_user.id not in get_admins(): return
    user_id = int(callback.data.split('_')[1])
    await callback.message.edit_caption(caption=callback.message.caption + "\n\n❌ **ОТКЛОНЕНО**")
    try:
        await bot.send_message(user_id, "❌ Ваша оплата отклонена. Обратитесь в '🆘 Поддержку'.")
    except:
        pass


# --- ПРОЧЕЕ ---
@dp.message(F.text == "🔗 Реф. ссылка")
async def ref_link(message: types.Message):
    bot_info = await bot.get_me()
    await message.answer(
        f"🔗 Ваша ссылка:\nhttps://t.me/{bot_info.username}?start={message.from_user.id}\n\nДает +10 запросов за друга!")


@dp.message(F.text == "🎁 Промокод")
async def promo_btn(message: types.Message, state: FSMContext):
    await state.set_state(BotStates.waiting_for_promo)
    await message.answer("Введите промокод:")


@dp.message(BotStates.waiting_for_promo)
async def process_promo(message: types.Message, state: FSMContext):
    promos = get_promocodes()
    if message.text in promos:
        promo = promos[message.text]
        conn = sqlite3.connect('users.db')
        if promo['type'] == 'requests':
            conn.execute("UPDATE users SET extra_requests = extra_requests + ? WHERE user_id=?",
                         (promo['value'], message.from_user.id))
            await message.answer(f"✅ Добавлено {promo['value']} запросов.")
        elif promo['type'] == 'sub':
            sub_id = promo['value']
            exp_date = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()

            sub_limit = next((s['limit'] for s in get_subs() if s['id'] == sub_id), 15)
            sub_name = next((s['name'] for s in get_subs() if s['id'] == sub_id), f"ID {sub_id}")

            conn.execute("UPDATE users SET sub_id=?, sub_exp=?, requests_left=?, last_reset=? WHERE user_id=?",
                         (sub_id, exp_date, sub_limit, datetime.date.today().isoformat(), message.from_user.id))
            await message.answer(f"✅ Выдана подписка **«{sub_name}»** на 30 дней. Лимиты обновлены!",
                                 parse_mode="Markdown")

        conn.commit()
        conn.close()
    else:
        await message.answer("❌ Неверный промокод.")
    await state.clear()


# ================= ЗАПУСК =================
async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())