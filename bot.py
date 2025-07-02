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

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_IDS = [int(os.getenv("ADMIN_CHAT_ID"))]
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
MONOBANK_CARD_NUMBER = os.getenv("MONOBANK_CARD_NUMBER")
DATABASE_URL = os.getenv("DATABASE_URL")

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
                status TEXT DEFAULT 'pending'
            );
        """)

async def save_product(data):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO products (user_id, username, name, price, photos, location, description, delivery)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """, data['user_id'], data['username'], data['name'], data['price'], data['photos'], data['location'], data['description'], data['delivery'])

async def get_next_pending_product():
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM products WHERE status = 'pending' ORDER BY id ASC LIMIT 1")

async def update_product_status(product_id, status):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE products SET status = $1 WHERE id = $2", status, product_id)

async def update_rotated_photos(product_id, new_file_ids):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE products SET photos = $1 WHERE id = $2", new_file_ids, product_id)

async def rotate_photos_and_notify(product):
    new_file_ids = []
    for file_id in product['photos']:
        file = await bot.get_file(file_id)
        file_path = file.file_path
        photo = await bot.download_file(file_path)
        image = Image.open(BytesIO(photo.read())).rotate(90, expand=True)
        buf = BytesIO()
        buf.name = 'rotated.jpg'
        image.save(buf, format='JPEG')
        buf.seek(0)
        msg = await bot.send_photo(product['user_id'], buf, caption="🔁 Повернуте фото")
        new_file_ids.append(msg.photo[-1].file_id)
    await update_rotated_photos(product['id'], new_file_ids)

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
        await message.answer("📷 Ви вже додали максимум 10 фото. Введіть місцезнаходження або пропустіть.")
        await CreateProduct.Location.set()
        return
    photos.append(message.photo[-1].file_id)
    await state.update_data(photos=photos)
    await message.answer(f"✅ Фото {len(photos)} додано. Надішліть ще або введіть місцезнаходження.")

@dp.message_handler(state=CreateProduct.Photos)
async def skip_photo(message: types.Message, state: FSMContext):
    await message.answer("📍 Введіть місцезнаходження або '-' щоб пропустити:")
    await CreateProduct.Location.set()

@dp.message_handler(state=CreateProduct.Location)
async def set_location(message: types.Message, state: FSMContext):
    await state.update_data(location=message.text)
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
    await message.answer(
        f"📦 Назва: {data['name']}\n💰 Ціна: {data['price']}\n📍 Локація: {data['location']}\n🚚 Доставка: {data['delivery']}\n📝 Опис: {data['description']}\n\n✅ Надіслати на модерацію? (Так/Ні)",
        reply_markup=types.ReplyKeyboardRemove())
    await CreateProduct.Confirm.set()

@dp.message_handler(state=CreateProduct.Confirm)
async def confirm_post(message: types.Message, state: FSMContext):
    if message.text.lower() != "так":
        await message.answer("❌ Скасовано.")
        await state.finish()
        return
    data = await state.get_data()
    product = data.copy()
    product['user_id'] = message.from_user.id
    product['username'] = message.from_user.username or ""
    await save_product(product)
    await message.answer(f"✅ Товар \"{data['name']}\" надіслано на модерацію. Очікуйте!")
    await state.finish()

@dp.callback_query_handler(lambda c: c.data in ["approve", "reject", "rotate"])
async def moderator_action(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Немає доступу", show_alert=True)
        return
    product = await get_next_pending_product()
    if not product:
        await callback.answer("Немає товарів у черзі")
        return

    if callback.data == "approve":
        media = [InputMediaPhoto(photo) for photo in product['photos'][:10]]
        caption = (
            f"📦 Назва: {product['name']}\n💰 Ціна: {product['price']}\n📍 Доставка: {product['delivery']}\n📝 Опис: {product['description']}\n👤 Продавець: @{product['username']}"
        )
        if media:
            media[0].caption = caption
            media[0].parse_mode = ParseMode.HTML
            await bot.send_media_group(CHANNEL_ID, media)
        await bot.send_message(product['user_id'], "✅ Ваш товар опубліковано!")
        await update_product_status(product['id'], "approved")

    elif callback.data == "reject":
        await bot.send_message(product['user_id'], "❌ Ваш товар було відхилено модератором.")
        await update_product_status(product['id'], "rejected")

    elif callback.data == "rotate":
        await rotate_photos_and_notify(product)
        await bot.send_message(product['user_id'], "🔄 Фото повернуто. Перевірте та подайте повторно, якщо потрібно.")
        await update_product_status(product['id'], "rotated")

@dp.message_handler(lambda m: m.text == "📖 Правила")
async def show_rules(message: types.Message):
    await message.answer(
        f"📌 Умови користування:\n\n* 🧾 Покупець оплачує доставку.\n* 💰 Продавець сплачує комісію платформи: 10%\n* 💳 Оплата комісії на Monobank: {MONOBANK_CARD_NUMBER}"
    )

@dp.message_handler(lambda m: m.text == "📋 Мої товари")
async def my_products(message: types.Message):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM products WHERE user_id = $1 ORDER BY id DESC", message.from_user.id)
        if not rows:
            await message.answer("У вас ще немає товарів.")
            return

        for row in rows[:10]:
            status = row['status']
            name = row['name']
            price = row['price']
            product_id = row['id']

            kb = InlineKeyboardMarkup(row_width=2)
            if status == "approved":
                kb.add(InlineKeyboardButton("👁 Переглянути", url=f"https://t.me/c/{str(CHANNEL_ID)[4:]}/{product_id}"))
                kb.add(InlineKeyboardButton("✅ Продано", callback_data=f"sold:{product_id}"))
            elif status == "pending":
                kb.add(InlineKeyboardButton("⌛ Очікує модерацію", callback_data="noop"))
            elif status == "rotated":
                kb.add(InlineKeyboardButton("🔁 Переопублікувати", callback_data=f"repost:{product_id}"))
            elif status == "sold":
                kb.add(InlineKeyboardButton("✅ Продано", callback_data="noop"))
            kb.add(InlineKeyboardButton("✏ Змінити ціну", callback_data=f"editprice:{product_id}"))
            kb.add(InlineKeyboardButton("🗑 Видалити", callback_data=f"delete:{product_id}"))

            await message.answer(f"📦 {name}\n💰 {price}\n📅 Статус: {status}", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith("sold:"))
async def mark_sold(callback: types.CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM products WHERE id = $1", product_id)
        if not row:
            await callback.answer("Не знайдено.")
            return
        price_text = row['price']
        try:
            price = int(''.join(filter(str.isdigit, price_text)))
            commission = max(10, round(price * 0.1))
        except:
            commission = 200
        await conn.execute("UPDATE products SET status = 'sold' WHERE id = $1", product_id)

    await bot.send_message(callback.from_user.id,
        f"💸 Комісія 10% = {commission} грн\n💳 Оплатіть на картку Monobank: {MONOBANK_CARD_NUMBER}")
    await callback.answer("Позначено як продано")

@dp.callback_query_handler(lambda c: c.data.startswith("delete:"))
async def delete_product(callback: types.CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM products WHERE id = $1 AND user_id = $2", product_id, callback.from_user.id)
    await callback.message.edit_text("🗑 Товар видалено.")
    await callback.answer("Видалено")

@dp.callback_query_handler(lambda c: c.data.startswith("editprice:"))
async def edit_price_prompt(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(editing_id=int(callback.data.split(":")[1]))
    await callback.message.answer("✏ Введіть нову ціну:")
    await state.set_state("edit_price")

@dp.message_handler(state="edit_price")
async def apply_new_price(message: types.Message, state: FSMContext):
    data = await state.get_data()
    product_id = data['editing_id']
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE products SET price = $1 WHERE id = $2 AND user_id = $3", message.text, product_id, message.from_user.id)
    await message.answer("💰 Ціну оновлено!")
    await state.finish()

@dp.callback_query_handler(lambda c: c.data.startswith("repost:"))
async def repost_product(callback: types.CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE products SET status = 'pending' WHERE id = $1 AND user_id = $2", product_id, callback.from_user.id)
    await callback.answer("Надіслано повторно на модерацію")
    await callback.message.edit_text("🔁 Товар повторно подано на модерацію")

@dp.message_handler(commands=["stats"])
async def show_stats(message: types.Message):
    async with db_pool.acquire() as conn:
        count_all = await conn.fetchval("SELECT COUNT(*) FROM products WHERE user_id = $1", message.from_user.id)
        count_pending = await conn.fetchval("SELECT COUNT(*) FROM products WHERE user_id = $1 AND status = 'pending'", message.from_user.id)
        count_approved = await conn.fetchval("SELECT COUNT(*) FROM products WHERE user_id = $1 AND status = 'approved'", message.from_user.id)
        count_sold = await conn.fetchval("SELECT COUNT(*) FROM products WHERE user_id = $1 AND status = 'sold'", message.from_user.id)

    await message.answer(f"Статистика ваших товарів:\nВсього: {count_all}\nОчікують модерації: {count_pending}\nОпубліковано: {count_approved}\nПродано: {count_sold}")

if __name__ == "__main__":
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    executor.start_polling(dp, skip_updates=True)
