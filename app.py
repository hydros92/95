import logging
import os
import asyncpg
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.utils import executor
from aiogram.types import InputMediaPhoto, ParseMode, InlineKeyboardMarkup, InlineKeyboardButton
from PIL import Image
from io import BytesIO
from dotenv import load_dotenv
from flask import Flask, request
import asyncio

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_CHAT_ID").split(',')]
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
MONOBANK_CARD_NUMBER = os.getenv("MONOBANK_CARD_NUMBER")
DATABASE_URL = os.getenv("DATABASE_URL")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())
db_pool = None

class CreateProduct(StatesGroup):
    Name = State()
    Price = State()
    Photos = State()
    Location = State()
    Description = State()
    Delivery = State()
    Confirm = State()

class EditPrice(StatesGroup):
    NewPrice = State()

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                username TEXT,
                name TEXT,
                price TEXT,
                photos TEXT[],
                location TEXT,
                description TEXT,
                delivery TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                repost_count INT DEFAULT 0,
                channel_message_id BIGINT DEFAULT NULL
            );
        """)
    logging.info("DB initialized.")

async def save_product(data):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO products (user_id, username, name, price, photos, location, description, delivery)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """, data['user_id'], data['username'], data['name'], data['price'], data['photos'], data['location'], data['description'], data['delivery'])
    logging.info(f"Product '{data['name']}' saved.")

async def update_product_status(product_id, status):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE products SET status = $1 WHERE id = $2", status, product_id)
    logging.info(f"Product {product_id} status updated to '{status}'.")

async def update_channel_message_id(product_id, message_id):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE products SET channel_message_id = $1 WHERE id = $2", message_id, product_id)
    logging.info(f"Product {product_id} channel_message_id updated.")

async def update_rotated_photos(product_id, new_file_ids):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE products SET photos = $1 WHERE id = $2", new_file_ids, product_id)
    logging.info(f"Product {product_id} photos updated after rotation.")

async def rotate_photos_and_notify(product):
    new_file_ids = []
    for file_id in product['photos']:
        try:
            file = await bot.get_file(file_id)
            file_path = file.file_path
            photo_bytes = await bot.download_file(file_path)
            image = Image.open(BytesIO(photo_bytes.read())).rotate(-90, expand=True)
            buf = BytesIO()
            buf.name = 'rotated.jpg'
            image.save(buf, format='JPEG')
            buf.seek(0)
            msg = await bot.send_photo(product['user_id'], buf, caption="🔁 Повернуте фото")
            new_file_ids.append(msg.photo[-1].file_id)
        except Exception as e:
            logging.error(f"Error rotating photo {file_id} for product {product['id']}: {e}")
            new_file_ids.append(file_id)
    await update_rotated_photos(product['id'], new_file_ids)
    await bot.send_message(product['user_id'], "🔄 Ваш товар оновлено. Фото повернуті.")
    await update_product_status(product['id'], "rotated")
    logging.info(f"User {product['user_id']} notified about photo rotation.")

@dp.message_handler(commands="start")
async def cmd_start(message: types.Message):
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add("📦 Додати товар", "📋 Мої товари")
    keyboard.add("📖 Правила")
    await message.answer("👋 Ласкаво просимо! Оберіть дію:", reply_markup=keyboard)

@dp.message_handler(lambda m: m.text == "📦 Додати товар")
async def add_product(message: types.Message):
    await message.answer("✏️ Введіть назву товару:", reply_markup=types.ReplyKeyboardRemove())
    await CreateProduct.Name.set()

@dp.message_handler(state=CreateProduct.Name)
async def set_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("💰 Введіть ціну (грн, $ або договірна):")
    await CreateProduct.Price.set()

@dp.message_handler(state=CreateProduct.Price)
async def set_price(message: types.Message, state: FSMContext):
    await state.update_data(price=message.text, photos=[])
    await message.answer("📷 Надішліть до 10 фото товару (по одному):")
    await CreateProduct.Photos.set()

@dp.message_handler(content_types=types.ContentType.PHOTO, state=CreateProduct.Photos)
async def upload_photos(message: types.Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    if len(photos) >= 10:
        await message.answer("📷 Максимум 10 фото додано. Введіть місцезнаходження або пропустіть.")
        await CreateProduct.Location.set()
        return
    photos.append(message.photo[-1].file_id)
    await state.update_data(photos=photos)
    await message.answer(f"✅ Фото {len(photos)} додано. Надішліть ще або введіть місцезнаходження.")

@dp.message_handler(lambda m: m.text in ["пропустити фото", "-"], state=CreateProduct.Photos)
async def skip_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    if not photos:
        await message.answer("Будь ласка, завантажте хоча б одне фото або введіть '-' для пропуску.")
        return
    await message.answer("📍 Введіть місцезнаходження або '-' щоб пропустити:")
    await CreateProduct.Location.set()

@dp.message_handler(state=CreateProduct.Location)
async def set_location(message: types.Message, state: FSMContext):
    location = message.text if message.text != "-" else "Не вказано"
    await state.update_data(location=location)
    await message.answer("📝 Введіть опис товару:")
    await CreateProduct.Description.set()

@dp.message_handler(state=CreateProduct.Description)
async def set_description(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text)
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("Наложка Укрпошта", "Наложка Нова пошта")
    await message.answer("🚚 Оберіть тип доставки:", reply_markup=kb)
    await CreateProduct.Delivery.set()

@dp.message_handler(state=CreateProduct.Delivery)
async def set_delivery(message: types.Message, state: FSMContext):
    await state.update_data(delivery=message.text)
    data = await state.get_data()
    confirm_text = (
        f"📦 Назва: {data['name']}\n"
        f"💰 Ціна: {data['price']}\n"
        f"📍 Локація: {data['location']}\n"
        f"🚚 Доставка: {data['delivery']}\n"
        f"📝 Опис: {data['description']}\n\n"
        f"✅ Надіслати на модерацію? (Так/Ні)"
    )
    await message.answer(confirm_text, reply_markup=types.ReplyKeyboardRemove())
    await CreateProduct.Confirm.set()

@dp.message_handler(state=CreateProduct.Confirm)
async def confirm_post(message: types.Message, state: FSMContext):
    if message.text.lower() != "так":
        await message.answer("❌ Скасовано. Ви можете почати заново з '📦 Додати товар'.",
                             reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add("📦 Додати товар", "📋 Мої товари", "📖 Правила"))
        await state.finish()
        return

    data = await state.get_data()
    product = data.copy()
    product['user_id'] = message.from_user.id
    product['username'] = message.from_user.username or f"id{message.from_user.id}"
    await save_product(product)
    await message.answer(f"✅ Товар \"{data['name']}\" надіслано на модерацію. Очікуйте!",
                         reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add("📦 Додати товар", "📋 Мої товари", "📖 Правила"))
    await state.finish()

    moderator_product_info = (
        f"🆕 Новий товар на модерацію!\n\n"
        f"📦 Назва: {product['name']}\n"
        f"💰 Ціна: {product['price']}\n"
        f"📝 Опис: {product['description']}\n"
        f"📍 Локація: {product['location']}\n"
        f"🚚 Доставка: {product['delivery']}\n"
        f"👤 Продавець: @{product['username']} (ID: {product['user_id']})"
    )
    moderator_keyboard = InlineKeyboardMarkup(row_width=2)
    moderator_keyboard.add(
        InlineKeyboardButton("✅ Опублікувати", callback_data=f"approve:{product['user_id']}:{product['name']}"),
        InlineKeyboardButton("❌ Відхилити", callback_data=f"reject:{product['user_id']}:{product['name']}")
    )
    moderator_keyboard.add(
        InlineKeyboardButton("🔄 Повернути фото", callback_data=f"rotate:{product['user_id']}:{product['name']}")
    )

    media_group = [InputMediaPhoto(file_id) for file_id in product['photos']]
    if media_group:
        try:
            await bot.send_media_group(ADMIN_IDS[0], media_group)
        except Exception as e:
            logging.error(f"Error sending media group to moderator: {e}")
            await bot.send_message(ADMIN_IDS[0], "Помилка при завантаженні фотографій. Перевірте вручну.")
    for admin_id in ADMIN_IDS:
        await bot.send_message(admin_id, moderator_product_info, reply_markup=moderator_keyboard)

@dp.callback_query_handler(lambda c: c.data.startswith(("approve:", "reject:", "rotate:")))
async def moderator_action(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Немає доступу", show_alert=True)
        return

    action, user_id_str, product_name = callback.data.split(":", 2)
    user_id = int(user_id_str)

    async with db_pool.acquire() as conn:
        product = await conn.fetchrow("SELECT * FROM products WHERE user_id = $1 AND name = $2 AND status = 'pending' ORDER BY created_at DESC LIMIT 1", user_id, product_name)

    if not product:
        await callback.answer("Цей товар вже оброблено або не знайдено.", show_alert=True)
        try:
            await callback.message.edit_text(f"Цей товар ({product_name}) вже оброблено або не знайдено.")
        except:
            pass
        return

    if action == "approve":
        media = [InputMediaPhoto(photo) for photo in product['photos']]
        caption = (
            f"📦 Назва: {product['name']}\n"
            f"💰 Ціна: {product['price']}\n"
            f"📍 Доставка: {product['delivery']}\n"
            f"📝 Опис: {product['description']}\n"
            f"👤 Продавець: @{product['username']}"
        )
        if media:
            media[0].caption = caption
            media[0].parse_mode = ParseMode.HTML
            try:
                sent_messages = await bot.send_media_group(CHANNEL_ID, media)
                if sent_messages:
                    await update_channel_message_id(product['id'], sent_messages[0].message_id)
              
                await update_product_status(product['id'], 'approved')
                await callback.message.edit_text(f"✅ Товар \"{product_name}\" опубліковано.")
                await bot.send_message(product['user_id'], f"✅ Ваш товар \"{product_name}\" опубліковано в каналі.")
            except Exception as e:
                await callback.message.edit_text(f"❌ Помилка публікації: {e}")
        else:
            await callback.message.edit_text("❌ Фото для товару не знайдено.")
    elif action == "reject":
        await update_product_status(product['id'], 'rejected')
        await callback.message.edit_text(f"❌ Товар \"{product_name}\" відхилено.")
        await bot.send_message(product['user_id'], f"❌ Ваш товар \"{product_name}\" відхилено модератором.")
    elif action == "rotate":
        # Запускаємо поворот фото
        await callback.message.edit_text(f"🔄 Повертаю фото товару \"{product_name}\"...")
        await rotate_photos_and_notify(product)
        await callback.message.edit_text(f"✅ Фото товару \"{product_name}\" повернуті.")

    await callback.answer()

@dp.message_handler(lambda m: m.text == "📋 Мої товари")
async def list_user_products(message: types.Message):
    async with db_pool.acquire() as conn:
        products = await conn.fetch("SELECT * FROM products WHERE user_id = $1 ORDER BY created_at DESC", message.from_user.id)
    if not products:
        await message.answer("У вас немає доданих товарів.")
        return
    for p in products:
        status_emoji = {
            'pending': '⏳',
            'approved': '✅',
            'rejected': '❌',
            'rotated': '🔄'
        }.get(p['status'], '')
        text = (f"{status_emoji} <b>{p['name']}</b>\n"
                f"💰 {p['price']}\n"
                f"📍 {p['location']}\n"
                f"🚚 {p['delivery']}\n"
                f"📝 {p['description']}\n"
                f"Статус: {p['status']}")
        kb = InlineKeyboardMarkup()
        if p['status'] == 'approved':
            kb.add(InlineKeyboardButton("Продано ✅", callback_data=f"sold_{p['id']}"))
        kb.add(InlineKeyboardButton("Повернути фото 🔄", callback_data=f"rotate_{p['id']}"))
        await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        if p['photos']:
            for file_id in p['photos']:
                await message.answer_photo(file_id)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("rotate_"))
async def rotate_user_photo_callback(callback: types.CallbackQuery):
    product_id = int(callback.data.split("_")[1])
    async with db_pool.acquire() as conn:
        product = await conn.fetchrow("SELECT * FROM products WHERE id = $1 AND user_id = $2", product_id, callback.from_user.id)
    if not product:
        await callback.answer("Товар не знайдено або ви не маєте доступу.", show_alert=True)
        return
    await callback.answer("Повертаю фото...", show_alert=False)
    await rotate_photos_and_notify(product)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("sold_"))
async def mark_product_sold(callback: types.CallbackQuery):
    product_id = int(callback.data.split("_")[1])
    async with db_pool.acquire() as conn:
        product = await conn.fetchrow("SELECT * FROM products WHERE id = $1 AND user_id = $2", product_id, callback.from_user.id)
    if not product:
        await callback.answer("Товар не знайдено або ви не маєте доступу.", show_alert=True)
        return
    # Розрахунок комісії 10%
    price_str = product['price'].replace(",", ".").replace(" ", "")
    commission = 0.0
    try:
        price_val = float(''.join(c for c in price_str if (c.isdigit() or c == '.')))
        commission = round(price_val * 0.10, 2)
    except Exception:
        commission = 0.0
    await update_product_status(product_id, 'sold')
    msg = (f"✅ Ваш товар \"{product['name']}\" позначено як проданий.\n"
           f"Комісія платформи 10%: {commission} грн.\n"
           f"Будь ласка, оплатіть комісію на картку Monobank:\n<b>{MONOBANK_CARD_NUMBER}</b>")
    await callback.message.answer(msg, parse_mode=ParseMode.HTML)
    await callback.answer("Позначено як проданий.")

@dp.message_handler(lambda m: m.text == "📖 Правила")
async def show_rules(message: types.Message):
    rules_text = (
        "📜 Правила роботи маркетплейсу:\n"
        "• Продавець оплачує комісію платформи 10% від вартості товару.\n"
        "• Покупець оплачує вартість доставки за своїм вибором.\n"
        "• Після натискання кнопки 'Продано' ви отримаєте інструкції щодо оплати комісії.\n"
        "• Заборонено публікувати заборонені товари.\n"
        "• Повернення товару на доопрацювання можливе через модератора.\n"
        "Дотримуйтесь правил, щоб успішно продавати!"
    )
    await message.answer(rules_text)

async def on_startup(dp):
    await init_db()
    logging.info("Бот запущено.")

# Flask + webhook

app = Flask(__name__)

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = types.Update.to_object(request.get_json(force=True))
    asyncio.run(dp.process_update(update))
    return "ok", 200

if __name__ == "__main__":
    from aiogram import executor
    # Для локального запуску polling (якщо потрібно)
    executor.start_polling(dp, on_startup=on_startup)




