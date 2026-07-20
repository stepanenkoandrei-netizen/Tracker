import asyncio
import logging
from datetime import datetime, timedelta
from collections import defaultdict
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg')
import io
import random
import os
import json
import threading
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from flask import Flask

# ==================== НАСТРОЙКИ КОНФИГУРАЦИИ ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8814120236:AAF66foOv1c9d9RllDKTsvOgXN58XuC2Mxk")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 855615522))
GOOGLE_SHEET_KEY = os.environ.get("GOOGLE_SHEET_KEY", "1gGp8GTzVHIQKes0UK_9kUKYc1mmPqyJfkjOcP7Y6EEs")
# ================================================================

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

# ==================== ПОДКЛЮЧЕНИЕ К GOOGLE SHEETS ====================

def get_google_creds():
    """Получение учетных данных Google из переменной окружения или файла"""
    try:
        google_creds_json = os.environ.get("GOOGLE_CREDS")
        if google_creds_json:
            creds_dict = json.loads(google_creds_json)
            scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
            return ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        
        if os.path.exists("cred.json"):
            scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
            return ServiceAccountCredentials.from_json_keyfile_name("cred.json", scope)
        
        logging.error("❌ Не найдены учетные данные Google")
        return None
    except Exception as e:
        logging.error(f"❌ Ошибка загрузки учетных данных: {e}")
        return None

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = get_google_creds()

if creds:
    try:
        client = gspread.authorize(creds)
        sheet = client.open_by_key(GOOGLE_SHEET_KEY)
        logging.info("✅ Успешное подключение к Google Sheets")
    except Exception as e:
        logging.error(f"❌ Ошибка подключения к Google Sheets: {e}")
        sheet = None
else:
    sheet = None
    logging.warning("⚠️ Бот запущен без подключения к Google Sheets")

# ==================== ИНИЦИАЛИЗАЦИЯ ЛИСТОВ ====================

def init_worksheets():
    if not sheet:
        return None, None
    
    try:
        operations_ws = sheet.worksheet("Operations")
    except:
        operations_ws = sheet.add_worksheet("Operations", 1000, 10)
        operations_ws.append_row(["Дата", "Время", "Тип", "Категория", "Сумма", "Комментарий"])
    
    try:
        settings_ws = sheet.worksheet("Settings")
    except:
        settings_ws = sheet.add_worksheet("Settings", 100, 10)
        settings_ws.append_row(["Категории расходов", "Категории доходов"])
        default_expenses = ["🍎 Продукты", "🚗 Транспорт", "🏠 ЖКХ", "☕ Кафе", 
                          "🛍️ Шопинг", "💊 Здоровье", "🎮 Развлечения", "📚 Образование", "🚕 Такси"]
        default_incomes = ["💰 Зарплата", "💳 Фриланс", "📈 Инвестиции", "🎁 Подарки", "💵 Прочее"]
        
        for i, cat in enumerate(default_expenses, 2):
            settings_ws.update_cell(i, 1, cat)
        for i, cat in enumerate(default_incomes, 2):
            settings_ws.update_cell(i, 2, cat)
    
    return operations_ws, settings_ws

try:
    operations_ws, settings_ws = init_worksheets()
except:
    operations_ws = None
    settings_ws = None
    logging.warning("⚠️ Не удалось инициализировать листы Google Sheets")

# ==================== СОСТОЯНИЯ FSM ====================

class CashStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_category = State()
    waiting_for_comment = State()
    waiting_for_quick_add = State()
    waiting_for_quick_type = State()

class AdminStates(StatesGroup):
    waiting_for_new_category = State()
    waiting_for_delete_category = State()
    waiting_for_report_type = State()
    waiting_for_edit_category = State()
    waiting_for_category_name = State()

class DeleteStates(StatesGroup):
    waiting_for_id = State()
    waiting_for_confirm = State()

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def get_main_keyboard(user_id):
    buttons = [
        [KeyboardButton(text="💰 Расход"), KeyboardButton(text="💵 Доход")],
        [KeyboardButton(text="⚡ Быстрый ввод"), KeyboardButton(text="📊 Баланс")],
        [KeyboardButton(text="📈 График"), KeyboardButton(text="📋 История")],
        [KeyboardButton(text="📅 Отчет"), KeyboardButton(text="🗑 Удалить")],
    ]
    if user_id == ADMIN_ID:
        buttons.append([KeyboardButton(text="⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def get_cancel_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True
    )

def get_comment_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Без комментария")],
            [KeyboardButton(text="❌ Отмена"), KeyboardButton(text="🔙 Назад")]
        ],
        resize_keyboard=True
    )

def get_categories(sheet_type="expenses"):
    if not settings_ws:
        return ["🍎 Продукты"] if sheet_type == "expenses" else ["💰 Зарплата"]
    
    try:
        records = settings_ws.get_all_values()
        if len(records) <= 1:
            return ["🍎 Продукты"] if sheet_type == "expenses" else ["💰 Зарплата"]
        
        col_idx = 1 if sheet_type == "expenses" else 2
        categories = []
        for row in records[1:]:
            if len(row) >= col_idx and row[col_idx-1]:
                categories.append(row[col_idx-1])
        return categories if categories else ["🍎 Продукты"] if sheet_type == "expenses" else ["💰 Зарплата"]
    except:
        return ["🍎 Продукты"] if sheet_type == "expenses" else ["💰 Зарплата"]

def get_categories_inline_kb(sheet_type):
    categories = get_categories(sheet_type)
    buttons = []
    row = []
    for i, cat in enumerate(categories):
        row.append(InlineKeyboardButton(text=cat, callback_data=f"cat_{cat}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="✏️ Свой вариант", callback_data="cat_custom")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_quick_amounts_kb():
    amounts = [100, 200, 500, 1000, 2000, 5000]
    buttons = []
    row = []
    for amount in amounts:
        row.append(KeyboardButton(text=str(amount)))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([KeyboardButton(text="❌ Отмена")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def get_type_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Расход", callback_data="type_expense")],
        [InlineKeyboardButton(text="💵 Доход", callback_data="type_income")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_main")]
    ])

def save_transaction(tx_type, category, amount, comment=""):
    if not operations_ws:
        return False
    
    try:
        now = datetime.now()
        operations_ws.append_row([
            now.strftime("%Y-%m-%d"),
            now.strftime("%H:%M:%S"),
            tx_type,
            category,
            float(amount),
            comment
        ])
        return True
    except:
        return False

def get_operations(days=30, category=None, tx_type=None):
    if not operations_ws:
        return []
    
    try:
        records = operations_ws.get_all_records()
        if not records:
            return []
        
        cutoff_date = datetime.now() - timedelta(days=days)
        result = []
        for record in records:
            try:
                record_date = datetime.strptime(record.get('Дата', ''), '%Y-%m-%d')
                if record_date >= cutoff_date:
                    if category and record.get('Категория') != category:
                        continue
                    if tx_type and record.get('Тип') != tx_type:
                        continue
                    result.append(record)
            except:
                continue
        return result
    except:
        return []

def format_currency(amount):
    return f"{amount:,.2f} ₽".replace(",", " ")

def get_frequent_categories(user_id=None, limit=6):
    try:
        operations = get_operations(30)
        if not operations:
            return []
        
        cat_counter = defaultdict(int)
        for op in operations:
            category = op.get('Категория', '')
            if category:
                cat_counter[category] += 1
        
        sorted_cats = sorted(cat_counter.items(), key=lambda x: x[1], reverse=True)
        return [cat for cat, _ in sorted_cats[:limit]]
    except:
        return []

def get_frequent_categories_kb(tx_type=None):
    frequent = get_frequent_categories()
    if not frequent:
        return None
    
    buttons = []
    row = []
    for cat in frequent[:4]:
        row.append(KeyboardButton(text=cat))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([KeyboardButton(text="📋 Все категории"), KeyboardButton(text="❌ Отмена")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

# ==================== ОБРАБОТЧИКИ КОМАНД ====================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    welcome_text = (
        "🌟 **Добро пожаловать в Финансового Бота!**\n\n"
        "📝 **Способы ввода:**\n"
        "1️⃣ **Быстрый ввод** - кнопка «⚡ Быстрый ввод»\n"
        "2️⃣ **Пошаговый ввод** - кнопки «💰 Расход» или «💵 Доход»\n"
        "3️⃣ **Командная строка** - `Сумма Категория` (например: `650 Продукты`)\n\n"
        "💡 **Подсказки:**\n"
        "• Бот запоминает часто используемые категории\n"
        "• Доступны быстрые суммы для ввода\n"
        "• Можно использовать эмодзи в категориях\n\n"
        "📊 **Доступные команды:**\n"
        "• 📊 Баланс - текущий баланс\n"
        "• 📈 График - динамика доходов и расходов\n"
        "• 📋 История - последние операции\n"
        "• 📅 Отчет - подробный отчет за период\n"
        "• 🗑 Удалить - удаление операций"
    )
    await message.answer(welcome_text, parse_mode="Markdown", reply_markup=get_main_keyboard(message.from_user.id))

# ==================== БЫСТРЫЙ ВВОД ====================

@dp.message(F.text == "⚡ Быстрый ввод")
async def quick_input_start(message: types.Message, state: FSMContext):
    await state.set_state(CashStates.waiting_for_quick_add)
    await message.answer(
        "⚡ **Быстрый ввод**\n\n"
        "Отправьте сообщение в одном из форматов:\n"
        "• `Сумма` - будет предложено выбрать категорию\n"
        "• `Сумма Категория` - сразу запишется\n"
        "• `Сумма Категория Комментарий` - с комментарием\n\n"
        "📌 **Примеры:**\n"
        "`1500` - затем выберете категорию\n"
        "`650 Продукты` - расход\n"
        "`2500 Зарплата` - доход\n"
        "`200 Такси Поездка в центр` - с комментарием\n\n"
        "Используйте быстрые суммы ниже:",
        parse_mode="Markdown",
        reply_markup=get_quick_amounts_kb()
    )

@dp.message(CashStates.waiting_for_quick_add)
async def process_quick_input(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await cancel_action(message, state)
        return
    
    try:
        amount = float(message.text.replace(',', '.'))
        await state.update_data(amount=amount)
        await state.update_data(quick_mode=True)
        
        await state.set_state(CashStates.waiting_for_quick_type)
        await message.answer(
            f"💰 Сумма: {format_currency(amount)}\n\nВыберите тип операции:",
            parse_mode="Markdown",
            reply_markup=get_type_kb()
        )
        return
    except ValueError:
        pass
    
    parts = message.text.strip().split(maxsplit=2)
    if len(parts) < 2:
        await message.answer(
            "❌ Неверный формат!\nИспользуйте: `Сумма Категория` или `Сумма`",
            parse_mode="Markdown",
            reply_markup=get_quick_amounts_kb()
        )
        return
    
    try:
        amount = float(parts[0].replace(',', '.'))
        category = parts[1].strip()
        comment = parts[2].strip() if len(parts) > 2 else ""
        
        income_cats = get_categories("incomes")
        tx_type = "Доход" if category in income_cats else "Расход"
        
        if save_transaction(tx_type, category, amount, comment):
            await state.clear()
            await message.answer(
                f"✅ **Запись успешно добавлена!**\n\n"
                f"📌 {tx_type}: {format_currency(amount)}\n"
                f"📂 Категория: {category}\n"
                f"📝 Комментарий: {comment if comment else '—'}",
                parse_mode="Markdown",
                reply_markup=get_main_keyboard(message.from_user.id)
            )
        else:
            await message.answer("❌ Ошибка сохранения. Попробуйте позже.")
            
    except ValueError:
        await message.answer(
            "❌ Ошибка! Первое значение должно быть числом.\nПример: `650 Продукты`",
            parse_mode="Markdown",
            reply_markup=get_quick_amounts_kb()
        )
    except Exception as e:
        logging.error(f"Ошибка быстрого ввода: {e}")
        await message.answer("❌ Произошла ошибка. Попробуйте еще раз.")

@dp.callback_query(CashStates.waiting_for_quick_type, F.data.startswith("type_"))
async def process_quick_type(callback: types.CallbackQuery, state: FSMContext):
    tx_type = "Расход" if "expense" in callback.data else "Доход"
    data = await state.get_data()
    amount = data.get('amount')
    
    if not amount:
        await callback.answer("❌ Ошибка! Попробуйте заново.")
        return
    
    await state.update_data(tx_type=tx_type)
    await state.update_data(quick_amount=amount)
    
    sheet_type = "expenses" if tx_type == "Расход" else "incomes"
    await state.set_state(CashStates.waiting_for_category)
    
    await callback.message.edit_text(
        f"💰 Сумма: {format_currency(amount)}\n"
        f"📌 Тип: {tx_type}\n\n"
        f"📁 **Выберите категорию** (или введите название вручную):",
        parse_mode="Markdown"
    )
    
    await callback.message.edit_reply_markup(
        reply_markup=get_categories_inline_kb(sheet_type)
    )
    
    frequent_kb = get_frequent_categories_kb(tx_type)
    if frequent_kb:
        await callback.message.answer(
            "📌 **Часто используемые категории:**",
            reply_markup=frequent_kb
        )
    
    await callback.answer()

# ==================== ПОШАГОВЫЙ ВВОД ====================

@dp.message(F.text.in_({"💰 Расход", "💵 Доход"}))
async def start_input(message: types.Message, state: FSMContext):
    tx_type = "Расход" if "Расход" in message.text else "Доход"
    await state.update_data(tx_type=tx_type)
    await state.set_state(CashStates.waiting_for_amount)
    
    await message.answer(
        f"💵 **Введите сумму** операции [**{tx_type}**]\n\n"
        f"Используйте кнопки быстрых сумм или введите свою:",
        parse_mode="Markdown",
        reply_markup=get_quick_amounts_kb()
    )

@dp.message(CashStates.waiting_for_amount)
async def process_amount(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await cancel_action(message, state)
        return
    
    try:
        amount = float(message.text.replace(',', '.'))
        await state.update_data(amount=amount)
        data = await state.get_data()
        
        sheet_type = "expenses" if data['tx_type'] == "Расход" else "incomes"
        await state.set_state(CashStates.waiting_for_category)
        
        await message.answer(
            f"💰 Сумма: {format_currency(amount)}\n"
            f"📌 Тип: {data['tx_type']}\n\n"
            f"📁 **Выберите категорию** (или введите название вручную):",
            parse_mode="Markdown"
        )
        
        await message.answer(
            "📁 **Категории:**",
            reply_markup=get_categories_inline_kb(sheet_type)
        )
        
        frequent_kb = get_frequent_categories_kb(data['tx_type'])
        if frequent_kb:
            await message.answer(
                "📌 **Часто используемые категории:**",
                reply_markup=frequent_kb
            )
            
    except ValueError:
        await message.answer(
            "❌ Неверный формат!\nПожалуйста, введите число (например: 1500.50)",
            reply_markup=get_quick_amounts_kb()
        )

# ==================== ОБРАБОТЧИКИ КАТЕГОРИЙ ====================

@dp.message(CashStates.waiting_for_category)
async def process_category_text(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await cancel_action(message, state)
        return
    
    if message.text == "📋 Все категории":
        data = await state.get_data()
        sheet_type = "expenses" if data.get('tx_type') == "Расход" else "incomes"
        await message.answer(
            "📁 **Все доступные категории:**",
            parse_mode="Markdown",
            reply_markup=get_categories_inline_kb(sheet_type)
        )
        return
    
    if message.text == "🔙 Назад":
        data = await state.get_data()
        amount = data.get('amount', 0)
        tx_type = data.get('tx_type', 'Расход')
        
        await state.set_state(CashStates.waiting_for_amount)
        await message.answer(
            f"🔙 Возврат к вводу суммы\n\n"
            f"💵 **Введите сумму** операции [**{tx_type}**]:",
            parse_mode="Markdown",
            reply_markup=get_quick_amounts_kb()
        )
        return
    
    category = message.text.strip()
    
    if not category:
        await message.answer(
            "❌ Название категории не может быть пустым!\n"
            "Пожалуйста, введите название категории или выберите из списка.",
            reply_markup=get_categories_inline_kb("expenses")
        )
        return
    
    await state.update_data(category=category)
    
    data = await state.get_data()
    amount = data.get('amount', 0)
    tx_type = data.get('tx_type', 'Расход')
    
    await state.set_state(CashStates.waiting_for_comment)
    await message.answer(
        f"✅ **Выбрано:**\n"
        f"💰 Сумма: {format_currency(amount)}\n"
        f"📌 Тип: {tx_type}\n"
        f"📂 Категория: {category}\n\n"
        f"✍️ **Введите комментарий**\n"
        f"Или нажмите кнопку «Без комментария»:",
        parse_mode="Markdown",
        reply_markup=get_comment_keyboard()
    )

@dp.callback_query(CashStates.waiting_for_category, F.data.startswith("cat_"))
async def process_category_callback(callback: types.CallbackQuery, state: FSMContext):
    if callback.data == "cat_custom":
        await callback.message.edit_text(
            "📝 **Введите название категории** вручную:",
            parse_mode="Markdown",
            reply_markup=None
        )
        await callback.answer()
        return
    
    category = callback.data.replace("cat_", "")
    await state.update_data(category=category)
    await state.set_state(CashStates.waiting_for_comment)
    
    data = await state.get_data()
    amount = data.get('amount', 0)
    tx_type = data.get('tx_type', 'Расход')
    
    await callback.message.edit_text(
        f"✅ **Выбрано:**\n"
        f"💰 Сумма: {format_currency(amount)}\n"
        f"📌 Тип: {tx_type}\n"
        f"📂 Категория: {category}\n\n"
        f"✍️ **Введите комментарий**\n"
        f"Или нажмите кнопку «Без комментария»:",
        parse_mode="Markdown"
    )
    await callback.message.answer(
        "Введите комментарий или нажмите кнопку:",
        reply_markup=get_comment_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.message.answer(
        "🔙 Возврат в главное меню",
        reply_markup=get_main_keyboard(callback.from_user.id)
    )
    await callback.answer()

# ==================== ОБРАБОТЧИК КОММЕНТАРИЯ ====================

@dp.message(CashStates.waiting_for_comment)
async def process_comment(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await cancel_action(message, state)
        return
    
    if message.text == "🔙 Назад":
        data = await state.get_data()
        amount = data.get('amount', 0)
        tx_type = data.get('tx_type', 'Расход')
        
        await state.set_state(CashStates.waiting_for_category)
        sheet_type = "expenses" if tx_type == "Расход" else "incomes"
        
        await message.answer(
            f"🔙 Возврат к выбору категории\n\n"
            f"💰 Сумма: {format_currency(amount)}\n"
            f"📌 Тип: {tx_type}\n\n"
            f"📁 **Выберите категорию:**",
            parse_mode="Markdown",
            reply_markup=get_categories_inline_kb(sheet_type)
        )
        return
    
    if message.text == "📝 Без комментария":
        comment = ""
    else:
        comment = message.text
    
    data = await state.get_data()
    
    if data.get('quick_amount'):
        amount = data.get('quick_amount')
        tx_type = data.get('tx_type')
        category = data.get('category')
        if save_transaction(tx_type, category, amount, comment):
            await state.clear()
            await message.answer(
                f"✅ **Запись успешно добавлена!**\n\n"
                f"📌 {tx_type}: {format_currency(amount)}\n"
                f"📂 Категория: {category}\n"
                f"📝 Комментарий: {comment if comment else '—'}",
                parse_mode="Markdown",
                reply_markup=get_main_keyboard(message.from_user.id)
            )
        else:
            await message.answer("❌ Ошибка сохранения. Попробуйте позже.")
        return
    
    tx_type = data.get('tx_type', 'Расход')
    category = data.get('category', 'Разное')
    amount = data.get('amount', 0)
    
    if save_transaction(tx_type, category, amount, comment):
        await state.clear()
        await message.answer(
            f"✅ **Запись успешно добавлена!**\n\n"
            f"📌 {tx_type}: {format_currency(amount)}\n"
            f"📂 Категория: {category}\n"
            f"📝 Комментарий: {comment if comment else '—'}",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
    else:
        await message.answer("❌ Ошибка сохранения. Попробуйте позже.")

# ==================== ОТЧЕТЫ ====================

@dp.message(F.text == "📊 Баланс")
async def send_balance(message: types.Message):
    operations = get_operations(30)
    if not operations:
        await message.answer("📭 Нет операций за последние 30 дней.")
        return
    
    total_income = 0
    total_expense = 0
    categories_exp = defaultdict(float)
    
    for op in operations:
        try:
            amount = float(op.get('Сумма', 0))
            category = op.get('Категория', 'Без категории')
            if op.get('Тип') == 'Доход':
                total_income += amount
            else:
                total_expense += amount
                categories_exp[category] += amount
        except:
            continue
    
    balance = total_income - total_expense
    
    text = (
        f"📊 **Финансовый отчет (последние 30 дней)**\n\n"
        f"💵 **Доходы:** {format_currency(total_income)}\n"
        f"💰 **Расходы:** {format_currency(total_expense)}\n"
        f"📈 **Баланс:** {format_currency(balance)}\n"
        f"📊 **Операций:** {len(operations)}\n\n"
    )
    
    if categories_exp:
        text += "📉 **Топ расходов:**\n"
        sorted_exp = sorted(categories_exp.items(), key=lambda x: x[1], reverse=True)[:5]
        for cat, amount in sorted_exp:
            percentage = (amount / total_expense * 100) if total_expense > 0 else 0
            bar_length = int(percentage / 5)
            bar = "█" * bar_length + "░" * (20 - bar_length)
            text += f"{cat}: {bar} {format_currency(amount)} ({percentage:.1f}%)\n"
    
    await message.answer(text, parse_mode="Markdown", reply_markup=get_main_keyboard(message.from_user.id))

# ==================== ЛИНЕЙНЫЙ ГРАФИК ДИНАМИКИ ====================

@dp.message(F.text == "📈 График")
async def send_chart(message: types.Message):
    """Отправляет линейный график динамики доходов и расходов по дням"""
    await message.answer("🔄 Строю график... Подождите секунду.")
    
    operations = get_operations(30)
    if not operations:
        await message.answer("📭 Нет данных для построения графика.")
        return
    
    # Собираем данные по дням
    daily_income = defaultdict(float)
    daily_expense = defaultdict(float)
    total_income = 0
    total_expense = 0
    
    for op in operations:
        try:
            amount = float(op.get('Сумма', 0))
            date = op.get('Дата', '')
            if op.get('Тип') == 'Доход':
                total_income += amount
                daily_income[date] += amount
            else:
                total_expense += amount
                daily_expense[date] += amount
        except:
            continue
    
    # Сортируем даты
    all_dates = sorted(set(daily_income.keys()) | set(daily_expense.keys()))
    if not all_dates:
        await message.answer("📭 Нет данных для построения графика.")
        return
    
    # Подготавливаем данные для графика
    income_values = [daily_income.get(date, 0) for date in all_dates]
    expense_values = [daily_expense.get(date, 0) for date in all_dates]
    
    # Создаем график
    fig, ax = plt.subplots(figsize=(14, 7))
    
    # Линии доходов и расходов
    ax.plot(all_dates, income_values, 'g-', label='Доходы', linewidth=2.5, marker='o', markersize=6, color='#2ecc71')
    ax.plot(all_dates, expense_values, 'r-', label='Расходы', linewidth=2.5, marker='s', markersize=6, color='#e74c3c')
    
    # Заливка области между линиями (если есть данные)
    if income_values and expense_values:
        ax.fill_between(all_dates, income_values, expense_values, 
                        where=[i > e for i, e in zip(income_values, expense_values)],
                        color='#2ecc71', alpha=0.1, label='Профицит')
        ax.fill_between(all_dates, income_values, expense_values,
                        where=[i < e for i, e in zip(income_values, expense_values)],
                        color='#e74c3c', alpha=0.1, label='Дефицит')
    
    # Настройка графика
    ax.set_title('📈 Динамика доходов и расходов за 30 дней', fontsize=16, fontweight='bold', pad=20)
    ax.set_xlabel('Дата', fontsize=12)
    ax.set_ylabel('Сумма (₽)', fontsize=12)
    ax.legend(loc='upper left', fontsize=11)
    ax.grid(True, alpha=0.3, linestyle='--')
    ax.tick_params(axis='x', rotation=45, labelsize=9)
    
    # Форматирование оси Y
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f'{int(x):,}'.replace(',', ' ')))
    
    # Добавляем итоговую статистику на график
    balance = total_income - total_expense
    stats_text = f"💰 Баланс: {format_currency(balance)}"
    ax.text(0.02, 0.98, stats_text, transform=ax.transAxes, fontsize=12,
            verticalalignment='top', bbox=dict(boxstyle='round', facecolor='white', alpha=0.8))
    
    plt.tight_layout()
    
    # Сохраняем в буфер
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
    buf.seek(0)
    plt.close()
    
    # Отправляем
    caption = (
        f"📊 **Динамика финансов за 30 дней**\n\n"
        f"💵 Доходы: {format_currency(total_income)}\n"
        f"💰 Расходы: {format_currency(total_expense)}\n"
        f"📈 Баланс: {format_currency(balance)}\n"
        f"📋 Операций: {len(operations)}"
    )
    
    await message.answer_photo(
        BufferedInputFile(buf.getvalue(), filename="chart.png"),
        caption=caption,
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(message.from_user.id)
    )

@dp.message(F.text == "📋 История")
async def show_history(message: types.Message):
    operations = get_operations(30)
    if not operations:
        await message.answer("📭 Нет операций за последние 30 дней.")
        return
    
    operations = operations[-10:][::-1]
    
    text = "📋 **Последние операции:**\n\n"
    for i, op in enumerate(operations[:10], 1):
        emoji = "💰" if op.get('Тип') == 'Доход' else "💸"
        text += f"{i}. {emoji} **{op.get('Тип')}:** {format_currency(float(op.get('Сумма', 0)))}\n"
        text += f"   📂 {op.get('Категория')}\n"
        if op.get('Комментарий'):
            text += f"   📝 {op.get('Комментарий')}\n"
        text += f"   📅 {op.get('Дата')} {op.get('Время', '')[:5]}\n"
        text += "   " + "-" * 25 + "\n\n"
    
    text += "💡 Для удаления операции используйте кнопку «🗑 Удалить»"
    
    await message.answer(text, parse_mode="Markdown", reply_markup=get_main_keyboard(message.from_user.id))

@dp.message(F.text == "📅 Отчет")
async def ask_period(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_report_type)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗓 За неделю (7 дней)", callback_data="report_7")],
        [InlineKeyboardButton(text="📅 За месяц (30 дней)", callback_data="report_30")],
        [InlineKeyboardButton(text="📆 За 3 месяца (90 дней)", callback_data="report_90")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
    ])
    await message.answer(
        "📊 **Выберите период для отчета:**",
        parse_mode="Markdown",
        reply_markup=kb
    )

@dp.callback_query(F.data.startswith("report_"))
async def generate_period_report(callback: types.CallbackQuery, state: FSMContext):
    """Генерирует отчет за выбранный период с разделением доходов и расходов"""
    await callback.answer("🔄 Генерирую отчет...")
    
    days = int(callback.data.split("_")[1])
    operations = get_operations(days)
    
    if not operations:
        await callback.message.edit_text(f"📭 Нет операций за выбранный период ({days} дней).")
        return
    
    # Разделяем доходы и расходы
    total_income = 0
    total_expense = 0
    categories_exp = defaultdict(float)
    categories_inc = defaultdict(float)
    
    for op in operations:
        try:
            amount = float(op.get('Сумма', 0))
            category = op.get('Категория', 'Без категории')
            if op.get('Тип') == 'Доход':
                total_income += amount
                categories_inc[category] += amount
            else:
                total_expense += amount
                categories_exp[category] += amount
        except:
            continue
    
    balance = total_income - total_expense
    
    # Создаем два графика: расходы и доходы
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))
    
    # 1. Расходы по категориям (только расходы) - улучшенные подписи
    if categories_exp:
        sorted_exp = sorted(categories_exp.items(), key=lambda x: x[1], reverse=True)[:8]
        cats, vals = zip(*sorted_exp) if sorted_exp else ([], [])
        colors = plt.cm.RdYlGn_r([i/len(vals) for i in range(len(vals))])
        wedges, texts, autotexts = ax1.pie(
            vals, 
            labels=cats, 
            autopct=lambda pct: f'{pct:.1f}%' if pct > 2 else '',
            colors=colors, 
            startangle=90,
            labeldistance=1.2,
            pctdistance=0.8,
            textprops={'fontsize': 9}
        )
        ax1.set_title('Расходы по категориям', fontsize=14, fontweight='bold')
    else:
        ax1.text(0.5, 0.5, 'Нет расходов', ha='center', va='center')
        ax1.set_title('Расходы по категориям', fontsize=14, fontweight='bold')
    
    # 2. Доходы по категориям (только доходы) - улучшенные подписи
    if categories_inc:
        sorted_inc = sorted(categories_inc.items(), key=lambda x: x[1], reverse=True)[:6]
        cats, vals = zip(*sorted_inc) if sorted_inc else ([], [])
        colors = plt.cm.Greens([i/len(vals) for i in range(len(vals))])
        wedges, texts, autotexts = ax2.pie(
            vals, 
            labels=cats, 
            autopct=lambda pct: f'{pct:.1f}%' if pct > 2 else '',
            colors=colors, 
            startangle=90,
            labeldistance=1.2,
            pctdistance=0.8,
            textprops={'fontsize': 9}
        )
        ax2.set_title('Доходы по категориям', fontsize=14, fontweight='bold')
    else:
        ax2.text(0.5, 0.5, 'Нет доходов', ha='center', va='center')
        ax2.set_title('Доходы по категориям', fontsize=14, fontweight='bold')
    
    plt.suptitle(f'📊 Финансовый отчет за {days} дней', fontsize=16, fontweight='bold')
    plt.tight_layout()
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
    buf.seek(0)
    plt.close()
    
    caption = (
        f"📊 **Отчет за {days} дней**\n\n"
        f"💵 Доходы: {format_currency(total_income)}\n"
        f"💰 Расходы: {format_currency(total_expense)}\n"
        f"📈 Баланс: {format_currency(balance)}\n"
        f"📋 Всего операций: {len(operations)}"
    )
    
    await callback.message.delete()
    await callback.message.answer_photo(
        BufferedInputFile(buf.getvalue(), filename="period_report.png"),
        caption=caption,
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(callback.from_user.id)
    )
    await state.clear()

# ==================== УДАЛЕНИЕ ====================

@dp.message(F.text == "🗑 Удалить")
async def delete_start(message: types.Message, state: FSMContext):
    operations = get_operations(30)
    if not operations:
        await message.answer("📭 Нет операций для удаления.")
        return
    
    await state.set_state(DeleteStates.waiting_for_id)
    
    text = "🗑 **Удаление операции**\n\nВыберите номер операции для удаления:\n\n"
    
    for i, op in enumerate(operations[-10:][::-1], 1):
        emoji = "💰" if op.get('Тип') == 'Доход' else "💸"
        text += f"{i}. {emoji} {op.get('Тип')}: {format_currency(float(op.get('Сумма', 0)))}\n"
        text += f"   📂 {op.get('Категория')}\n"
        text += f"   📅 {op.get('Дата')} {op.get('Время', '')[:5]}\n\n"
    
    text += "Введите номер операции для удаления\nИли отправьте ❌ Отмена"
    
    await message.answer(text, parse_mode="Markdown", reply_markup=get_cancel_keyboard())

@dp.message(DeleteStates.waiting_for_id)
async def delete_confirm(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await cancel_action(message, state)
        return
    
    if message.text == "🔙 Назад":
        await state.clear()
        await message.answer("🔙 Возврат в главное меню", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
    try:
        index = int(message.text) - 1
        operations = get_operations(30)
        
        if index >= len(operations):
            await message.answer("❌ Неверный номер операции. Попробуйте снова.")
            return
        
        operation = operations[-(index+1)]
        
        await state.update_data(delete_operation=operation)
        await state.set_state(DeleteStates.waiting_for_confirm)
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, удалить", callback_data="confirm_delete")],
            [InlineKeyboardButton(text="❌ Нет, отмена", callback_data="back_to_main")]
        ])
        
        await message.answer(
            f"⚠️ **Вы уверены, что хотите удалить операцию?**\n\n"
            f"📌 {operation.get('Тип')}: {format_currency(float(operation.get('Сумма', 0)))}\n"
            f"📂 Категория: {operation.get('Категория')}\n"
            f"📅 {operation.get('Дата')} {operation.get('Время', '')[:5]}\n\n"
            f"Это действие нельзя отменить!",
            parse_mode="Markdown",
            reply_markup=kb
        )
            
    except ValueError:
        await message.answer("❌ Введите число.")
    except Exception as e:
        logging.error(f"Ошибка: {e}")
        await message.answer("❌ Произошла ошибка.")

@dp.callback_query(F.data == "confirm_delete", DeleteStates.waiting_for_confirm)
async def confirm_delete(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    operation = data.get('delete_operation')
    
    if not operation:
        await callback.answer("❌ Ошибка! Попробуйте заново.")
        return
    
    try:
        records = operations_ws.get_all_values()
        for i, record in enumerate(records):
            if (record[0] == operation.get('Дата') and 
                record[1] == operation.get('Время') and
                record[2] == operation.get('Тип') and
                record[3] == operation.get('Категория') and
                float(record[4]) == float(operation.get('Сумма', 0))):
                operations_ws.delete_rows(i+1)
                break
        
        await state.clear()
        await callback.message.edit_text(
            f"✅ **Операция удалена!**\n\n"
            f"📌 {operation.get('Тип')}: {format_currency(float(operation.get('Сумма', 0)))}\n"
            f"📂 Категория: {operation.get('Категория')}\n"
            f"📅 {operation.get('Дата')}"
        )
        await callback.message.answer(
            "Главное меню:",
            reply_markup=get_main_keyboard(callback.from_user.id)
        )
        
    except Exception as e:
        logging.error(f"Ошибка удаления: {e}")
        await callback.message.answer("❌ Ошибка удаления операции.")
    
    await callback.answer()

# ==================== АДМИН-ПАНЕЛЬ ====================

@dp.message(F.text == "⚙️ Админ-панель", F.from_user.id == ADMIN_ID)
async def admin_panel(message: types.Message):
    expense_cats = get_categories("expenses")
    income_cats = get_categories("incomes")
    
    text = "⚙️ **Админ-панель управления**\n\n"
    text += "📌 **Категории расходов:**\n"
    for i, cat in enumerate(expense_cats, 1):
        text += f"{i}. {cat}\n"
    text += "\n📌 **Категории доходов:**\n"
    for i, cat in enumerate(income_cats, 1):
        text += f"{i}. {cat}\n"
    text += "\nВыберите действие:"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить категорию расходов", callback_data="adm_add_exp")],
        [InlineKeyboardButton(text="➕ Добавить категорию доходов", callback_data="adm_add_inc")],
        [InlineKeyboardButton(text="🗑 Удалить категорию", callback_data="adm_delete_cat")],
        [InlineKeyboardButton(text="🔄 Обновить список", callback_data="adm_refresh")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
    ])
    await message.answer(text, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(F.data.startswith("adm_add_"), F.from_user.id == ADMIN_ID)
async def admin_add_category(callback: types.CallbackQuery, state: FSMContext):
    cat_type = "expenses" if "exp" in callback.data else "incomes"
    await state.update_data(admin_cat_type=cat_type)
    await state.set_state(AdminStates.waiting_for_new_category)
    
    type_text = "расходов" if cat_type == "expenses" else "доходов"
    existing_cats = get_categories(cat_type)
    
    text = f"📝 **Добавление новой категории {type_text}**\n\n"
    text += "📌 **Существующие категории:**\n"
    for i, cat in enumerate(existing_cats, 1):
        text += f"{i}. {cat}\n"
    text += f"\nОтправьте название категории с эмодзи\n"
    text += f"Пример: `🍕 Рестораны` или `🚕 Такси`\n\n"
    text += f"Или отправьте ❌ Отмена для выхода"
    
    await callback.message.edit_text(text)
    await callback.answer()

@dp.message(AdminStates.waiting_for_new_category, F.from_user.id == ADMIN_ID)
async def admin_add_finish(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await cancel_action(message, state)
        return
    
    new_cat = message.text.strip()
    data = await state.get_data()
    cat_type = data['admin_cat_type']
    
    if not new_cat:
        await message.answer("❌ Название не может быть пустым!")
        return
    
    existing_cats = get_categories(cat_type)
    if new_cat in existing_cats:
        await message.answer(f"⚠️ Категория '{new_cat}' уже существует!\n\nПожалуйста, введите другое название.")
        return
    
    try:
        records = settings_ws.get_all_values()
        col = 1 if cat_type == "expenses" else 2
        
        row_num = 2
        for row in records[1:]:
            if len(row) < col or not row[col-1]:
                break
            row_num += 1
        
        settings_ws.update_cell(row_num, col, new_cat)
        
        await state.clear()
        
        updated_cats = get_categories(cat_type)
        type_text = "расходов" if cat_type == "expenses" else "доходов"
        
        text = f"✅ **Категория успешно добавлена!**\n\n"
        text += f"📌 {new_cat}\n"
        text += f"📂 Раздел: {type_text}\n\n"
        text += f"📋 **Обновленный список {type_text}:**\n"
        for i, cat in enumerate(updated_cats, 1):
            text += f"{i}. {cat}\n"
        
        await message.answer(text, parse_mode="Markdown", reply_markup=get_main_keyboard(message.from_user.id))
    except Exception as e:
        logging.error(f"Ошибка добавления категории: {e}")
        await message.answer("❌ Ошибка добавления категории. Попробуйте позже.")

@dp.callback_query(F.data == "adm_delete_cat", F.from_user.id == ADMIN_ID)
async def admin_delete_cat_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_delete_category)
    
    expense_cats = get_categories("expenses")
    income_cats = get_categories("incomes")
    
    text = "🗑 **Удаление категории**\n\n"
    text += "📌 **Категории расходов:**\n"
    for i, cat in enumerate(expense_cats, 1):
        text += f"{i}. {cat}\n"
    text += "\n📌 **Категории доходов:**\n"
    for i, cat in enumerate(income_cats, 1):
        text += f"{i}. {cat}\n"
    text += f"\n📝 Введите точное название категории для удаления\n"
    text += "Или отправьте ❌ Отмена для выхода"
    
    await callback.message.edit_text(text)
    await callback.answer()

@dp.message(AdminStates.waiting_for_delete_category, F.from_user.id == ADMIN_ID)
async def admin_delete_finish(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await cancel_action(message, state)
        return
    
    cat_to_delete = message.text.strip()
    
    if not cat_to_delete:
        await message.answer("❌ Введите название категории!")
        return
    
    all_cats = get_categories("expenses") + get_categories("incomes")
    if cat_to_delete not in all_cats:
        await message.answer(f"⚠️ Категория '{cat_to_delete}' не найдена!\n\nПожалуйста, проверьте название.")
        return
    
    try:
        records = settings_ws.get_all_values()
        found = False
        
        for i, row in enumerate(records, start=1):
            if row and cat_to_delete in row:
                col = row.index(cat_to_delete) + 1
                settings_ws.update_cell(i, col, "")
                found = True
                break
        
        if found:
            await state.clear()
            
            expense_cats = get_categories("expenses")
            income_cats = get_categories("incomes")
            
            text = f"✅ **Категория удалена!**\n\n"
            text += f"📌 {cat_to_delete}\n\n"
            text += "📌 **Обновленные категории расходов:**\n"
            for i, cat in enumerate(expense_cats, 1):
                text += f"{i}. {cat}\n"
            text += "\n📌 **Обновленные категории доходов:**\n"
            for i, cat in enumerate(income_cats, 1):
                text += f"{i}. {cat}\n"
            
            await message.answer(text, parse_mode="Markdown", reply_markup=get_main_keyboard(message.from_user.id))
        else:
            await message.answer("❌ Не удалось найти категорию в таблице.")
            
    except Exception as e:
        logging.error(f"Ошибка удаления категории: {e}")
        await message.answer("❌ Ошибка удаления категории. Попробуйте позже.")

@dp.callback_query(F.data == "adm_refresh", F.from_user.id == ADMIN_ID)
async def admin_refresh(callback: types.CallbackQuery):
    await callback.answer("🔄 Обновляю список...")
    
    expense_cats = get_categories("expenses")
    income_cats = get_categories("incomes")
    
    text = "✅ **Список обновлен!**\n\n"
    text += "📌 **Категории расходов:**\n"
    for i, cat in enumerate(expense_cats, 1):
        text += f"{i}. {cat}\n"
    text += "\n📌 **Категории доходов:**\n"
    for i, cat in enumerate(income_cats, 1):
        text += f"{i}. {cat}\n"
    
    await callback.message.edit_text(text)

# ==================== ОБЩИЕ ОБРАБОТЧИКИ ====================

@dp.message(F.text == "❌ Отмена")
async def cancel_action(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Действие отменено", reply_markup=get_main_keyboard(message.from_user.id))

@dp.message(F.text == "🔙 Назад")
async def go_back(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    
    if current_state == CashStates.waiting_for_comment:
        data = await state.get_data()
        amount = data.get('amount', 0)
        tx_type = data.get('tx_type', 'Расход')
        
        await state.set_state(CashStates.waiting_for_category)
        sheet_type = "expenses" if tx_type == "Расход" else "incomes"
        
        await message.answer(
            f"🔙 Возврат к выбору категории\n\n"
            f"💰 Сумма: {format_currency(amount)}\n"
            f"📌 Тип: {tx_type}\n\n"
            f"📁 **Выберите категорию:**",
            parse_mode="Markdown",
            reply_markup=get_categories_inline_kb(sheet_type)
        )
    elif current_state == CashStates.waiting_for_category:
        data = await state.get_data()
        tx_type = data.get('tx_type', 'Расход')
        
        await state.set_state(CashStates.waiting_for_amount)
        await message.answer(
            f"🔙 Возврат к вводу суммы\n\n"
            f"💵 **Введите сумму** операции [**{tx_type}**]:",
            parse_mode="Markdown",
            reply_markup=get_quick_amounts_kb()
        )
    else:
        await cancel_action(message, state)

@dp.message(F.text & ~F.text.startswith('/') & ~F.text.in_({"💰 Расход", "💵 Доход", "⚡ Быстрый ввод", "📊 Баланс", "📈 График", "📋 История", "📅 Отчет", "🗑 Удалить", "⚙️ Админ-панель", "❌ Отмена", "🔙 Назад", "📝 Без комментария", "📋 Все категории"}))
async def quick_text_input(message: types.Message):
    parts = message.text.strip().split(maxsplit=1)
    
    if len(parts) < 2:
        await message.answer(
            "❌ Неверный формат!\nИспользуйте: `Сумма Категория`\nПример: `650 Продукты`\n\nИли используйте кнопку «⚡ Быстрый ввод» для пошагового ввода.",
            parse_mode="Markdown"
        )
        return
    
    try:
        amount = float(parts[0].replace(',', '.'))
        category = parts[1].strip()
        
        all_cats = get_categories("expenses") + get_categories("incomes")
        if category not in all_cats:
            await message.answer(
                f"⚠️ Категория '{category}' не найдена!\n\n"
                f"Доступные категории:\n{', '.join(all_cats[:10])}\n\n"
                f"Используйте кнопку «⚡ Быстрый ввод» для выбора категории.",
                parse_mode="Markdown"
            )
            return
        
        income_cats = get_categories("incomes")
        tx_type = "Доход" if category in income_cats else "Расход"
        
        if save_transaction(tx_type, category, amount):
            await message.answer(
                f"✅ **Успешно внесено!**\n"
                f"📌 {tx_type}: {format_currency(amount)}\n"
                f"📂 Категория: {category}",
                parse_mode="Markdown",
                reply_markup=get_main_keyboard(message.from_user.id)
            )
        else:
            await message.answer("❌ Ошибка сохранения. Попробуйте позже.")
            
    except ValueError:
        await message.answer(
            "❌ Ошибка! Первое значение должно быть числом.\nПример: `650 Продукты`",
            parse_mode="Markdown"
        )

# ==================== ЗАПУСК БОТА И ВЕБ-СЕРВЕРА ДЛЯ RENDER ====================

async def main():
    logging.info("🚀 Бот запущен! Ожидаю сообщения...")
    await dp.start_polling(bot)

flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "🤖 Финансовый бот работает!"

@flask_app.route('/health')
def health():
    return "OK"

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host='0.0.0.0', port=port, debug=False)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logging.info("🚀 Веб-сервер запущен в фоновом режиме")
    asyncio.run(main())
