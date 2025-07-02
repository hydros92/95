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
from flask import Flask, request # Додано імпорт Flask та request
import asyncio # Додано імпорт asyncio

# Завантажуємо змінні оточення з файлу .env
load_dotenv()

# Отримуємо токени та ID з змінних оточення
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# Розділяємо ADMIN_CHAT_ID, якщо їх декілька, та конвертуємо в int
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_CHAT_ID").split(',')]
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
MONOBANK_CARD_NUMBER = os.getenv("MONOBANK_CARD_NUMBER")
DATABASE_URL = os.getenv("DATABASE_URL")

# Налаштування логування
logging.basicConfig(level=logging.INFO)

# Ініціалізація бота та диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())
db_pool = None # Пул підключень до бази даних

# Клас станів для створення нового товару
class CreateProduct(StatesGroup):
    Name = State()
    Price = State()
    Photos = State()
    Location = State()
    Description = State()
    Delivery = State()
    Confirm = State()

# Клас станів для редагування ціни
class EditPrice(StatesGroup):
    NewPrice = State()

# Функція для ініціалізації бази даних
async def init_db():
    global db_pool
    # Створюємо пул підключень до PostgreSQL
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        # Створюємо таблицю products, якщо вона не існує
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
        logging.info("База даних ініціалізована успішно.")

# Функція для збереження нового товару в базу даних
async def save_product(data):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO products (user_id, username, name, price, photos, location, description, delivery)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """, data['user_id'], data['username'], data['name'], data['price'], data['photos'], data['location'], data['description'], data['delivery'])
        logging.info(f"Товар '{data['name']}' збережено в БД.")

# Функція для отримання наступного товару на модерацію
async def get_next_pending_product():
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM products WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1")

# Функція для оновлення статусу товару
async def update_product_status(product_id, status):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE products SET status = $1 WHERE id = $2", status, product_id)
        logging.info(f"Статус товару {product_id} оновлено на '{status}'.")

# Функція для оновлення ID повідомлення в каналі
async def update_channel_message_id(product_id, message_id):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE products SET channel_message_id = $1 WHERE id = $2", message_id, product_id)
        logging.info(f"ID повідомлення в каналі для товару {product_id} оновлено на {message_id}.")

# Функція для оновлення фотографій після повороту
async def update_rotated_photos(product_id, new_file_ids):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE products SET photos = $1 WHERE id = $2", new_file_ids, product_id)
        logging.info(f"Фотографії для товару {product_id} оновлено після повороту.")

# Функція для повороту фотографій та повідомлення користувача
async def rotate_photos_and_notify(product):
    new_file_ids = []
    for file_id in product['photos']:
        try:
            file = await bot.get_file(file_id)
            file_path = file.file_path
            photo_bytes = await bot.download_file(file_path)
            image = Image.open(BytesIO(photo_bytes.read())).rotate(-90, expand=True) # Поворот на 90 градусів проти годинникової стрілки
            buf = BytesIO()
            buf.name = 'rotated.jpg'
            image.save(buf, format='JPEG')
            buf.seek(0)
            # Надсилаємо повернуте фото користувачу, щоб отримати новий file_id
            msg = await bot.send_photo(product['user_id'], buf, caption="🔁 Повернуте фото")
            new_file_ids.append(msg.photo[-1].file_id)
        except Exception as e:
            logging.error(f"Помилка при повороті фото {file_id} для товару {product['id']}: {e}")
            # Якщо виникла помилка, залишаємо старий file_id
            new_file_ids.append(file_id)
    await update_rotated_photos(product['id'], new_file_ids)
    await bot.send_message(product['user_id'],
                           "🔄 Ваш товар оновлено.\n📸 Фото були повернуті для правильного відображення.\nПеревірте та подайте повторно, якщо потрібно.")
    await update_product_status(product['id'], "rotated")
    logging.info(f"Користувача {product['user_id']} повідомлено про поворот фото.")

# Обробник команди /start
@dp.message_handler(commands="start")
async def cmd_start(message: types.Message):
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add("📦 Додати товар", "📋 Мої товари")
    keyboard.add("📖 Правила")
    await message.answer("👋 Ласкаво просимо! Оберіть дію:", reply_markup=keyboard)
    logging.info(f"Користувач {message.from_user.id} запустив бота.")

# Обробник кнопки "Додати товар"
@dp.message_handler(lambda m: m.text == "📦 Додати товар")
async def add_product(message: types.Message):
    await message.answer("✏️ Введіть назву товару:", reply_markup=types.ReplyKeyboardRemove())
    await CreateProduct.Name.set()
    logging.info(f"Користувач {message.from_user.id} почав додавати товар.")

# Крок 1: Введення назви товару
@dp.message_handler(state=CreateProduct.Name)
async def set_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("💰 Введіть ціну (грн, $ або договірна):")
    await CreateProduct.Price.set()
    logging.info(f"Користувач {message.from_user.id} ввів назву товару: {message.text}")

# Крок 2: Введення ціни
@dp.message_handler(state=CreateProduct.Price)
async def set_price(message: types.Message, state: FSMContext):
    await state.update_data(price=message.text, photos=[])
    await message.answer("📷 Надішліть до 10 фото товару (по одному):")
    await CreateProduct.Photos.set()
    logging.info(f"Користувач {message.from_user.id} ввів ціну: {message.text}")

# Крок 3: Завантаження фото
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
    logging.info(f"Користувач {message.from_user.id} додав фото. Всього фото: {len(photos)}")

# Крок 3: Пропуск завантаження фото
@dp.message_handler(lambda m: m.text == "пропустити фото" or m.text == "-", state=CreateProduct.Photos)
async def skip_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    if not photos:
        await message.answer("Будь ласка, завантажте хоча б одне фото або введіть '-' для пропуску.")
        return
    await message.answer("📍 Введіть місцезнаходження або '-' щоб пропустити:")
    await CreateProduct.Location.set()
    logging.info(f"Користувач {message.from_user.id} пропустив додавання фото.")

# Крок 4: Введення геолокації
@dp.message_handler(state=CreateProduct.Location)
async def set_location(message: types.Message, state: FSMContext):
    location = message.text if message.text != "-" else "Не вказано"
    await state.update_data(location=location)
    await message.answer("📝 Введіть опис товару:")
    await CreateProduct.Description.set()
    logging.info(f"Користувач {message.from_user.id} ввів місцезнаходження: {location}")

# Крок 5: Введення опису
@dp.message_handler(state=CreateProduct.Description)
async def set_description(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text)
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("Наложка Укрпошта", "Наложка Нова пошта")
    await message.answer("🚚 Оберіть тип доставки:", reply_markup=kb)
    await CreateProduct.Delivery.set()
    logging.info(f"Користувач {message.from_user.id} обрав доставку: {message.text}")

# Крок 6: Вибір доставки
@dp.message_handler(state=CreateProduct.Delivery)
async def set_delivery(message: types.Message, state: FSMContext):
    await state.update_data(delivery=message.text)
    data = await state.get_data()
    # Формуємо текст для підтвердження
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
    logging.info(f"Користувач {message.from_user.id} обрав доставку: {message.text}")

# Крок 7: Підтвердження
@dp.message_handler(state=CreateProduct.Confirm)
async def confirm_post(message: types.Message, state: FSMContext):
    if message.text.lower() != "так":
        await message.answer("❌ Скасовано. Ви можете почати створення нового оголошення за допомогою '📦 Додати товар'.",
                             reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add("📦 Додати товар", "📋 Мої товари", "📖 Правила"))
        await state.finish()
        logging.info(f"Користувач {message.from_user.id} скасував створення товару.")
        return

    data = await state.get_data()
    product = data.copy()
    product['user_id'] = message.from_user.id
    product['username'] = message.from_user.username or f"id{message.from_user.id}" # Використовуємо ID, якщо юзернейм відсутній
    await save_product(product)
    await message.answer(f"✅ Товар \"{data['name']}\" надіслано на модерацію. Очікуйте!",
                         reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add("📦 Додати товар", "📋 Мої товари", "📖 Правила"))
    await state.finish()
    logging.info(f"Товар '{data['name']}' від користувача {message.from_user.id} надіслано на модерацію.")

    # Повідомлення модераторам
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
    # Змінено callback_data, щоб передавати ID товару, а не ім'я, для уникнення проблем з унікальністю
    moderator_keyboard.add(
        InlineKeyboardButton("✅ Опублікувати", callback_data=f"approve:{product['user_id']}:{product['name']}"),
        InlineKeyboardButton("❌ Відхилити", callback_data=f"reject:{product['user_id']}:{product['name']}")
    )
    moderator_keyboard.add(
        InlineKeyboardButton("🔄 Повернути фото", callback_data=f"rotate:{product['user_id']}:{product['name']}")
    )

    media_group = [InputMediaPhoto(file_id) for file_id in product['photos']]
    if media_group:
        # Надсилаємо фотографії модератору
        try:
            # Надсилаємо медіагрупу, якщо є фото
            await bot.send_media_group(ADMIN_IDS[0], media_group) # Надсилаємо першому адміну
        except Exception as e:
            logging.error(f"Помилка при відправці медіагрупи модератору: {e}")
            await bot.send_message(ADMIN_IDS[0], "Помилка при завантаженні фотографій. Будь ласка, перевірте вручну.")
    
    for admin_id in ADMIN_IDS:
        await bot.send_message(admin_id, moderator_product_info, reply_markup=moderator_keyboard)
        logging.info(f"Модератору {admin_id} надіслано повідомлення про новий товар.")


# Обробник дій модератора (Опублікувати, Відхилити, Повернути фото)
@dp.callback_query_handler(lambda c: c.data.startswith(("approve:", "reject:", "rotate:")))
async def moderator_action(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Немає доступу", show_alert=True)
        return

    action, user_id_str, product_name = callback.data.split(":", 2)
    user_id = int(user_id_str)

    # Отримуємо товар, який відповідає user_id та product_name
    async with db_pool.acquire() as conn:
        product = await conn.fetchrow("SELECT * FROM products WHERE user_id = $1 AND name = $2 AND status = 'pending' ORDER BY created_at DESC LIMIT 1", user_id, product_name)

    if not product:
        await callback.answer("Цей товар вже оброблено або не знайдено.", show_alert=True)
        # Оновлюємо повідомлення модератора, щоб уникнути повторних натискань
        try:
            await callback.message.edit_text(f"Цей товар ({product_name}) вже оброблено або не знайдено.")
        except Exception as e:
            logging.warning(f"Не вдалося відредагувати повідомлення модератора: {e}")
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
                # Публікуємо товар в каналі
                sent_messages = await bot.send_media_group(CHANNEL_ID, media)
                if sent_messages:
                    # Зберігаємо ID першого повідомлення групи для кнопки "Переглянути в каналі"
                    await update_channel_message_id(product['id'], sent_messages[0].message_id)
                await bot.send_message(product['user_id'], f"✅ Ваш товар \"{product['name']}\" опубліковано!")
                await update_product_status(product['id'], "approved")
                await callback.answer("Товар опубліковано.")
                await callback.message.edit_text(f"✅ Товар \"{product['name']}\" опубліковано.")
                logging.info(f"Товар {product['id']} опубліковано в каналі.")
            except Exception as e:
                logging.error(f"Помилка при публікації товару {product['id']} в каналі: {e}")
                await callback.answer("Помилка при публікації товару.", show_alert=True)
        else:
            await callback.answer("Немає фотографій для публікації.", show_alert=True)
            await callback.message.edit_text(f"❌ Товар \"{product['name']}\" не може бути опублікований без фото.")


    elif action == "reject":
        await bot.send_message(product['user_id'], f"❌ Ваш товар \"{product['name']}\" було відхилено модератором. Будь ласка, перевірте правила.")
        await update_product_status(product['id'], "rejected")
        await callback.answer("Товар відхилено.")
        await callback.message.edit_text(f"❌ Товар \"{product['name']}\" відхилено.")
        logging.info(f"Товар {product['id']} відхилено.")

    elif action == "rotate":
        # Відкривається режим редагування зображень
        await callback.answer("Відкриваю режим редагування фото.")
        await rotate_photos_and_notify(product)
        await callback.message.edit_text(f"🔄 Фото для товару \"{product['name']}\" повернуто. Користувача повідомлено.")
        logging.info(f"Фото для товару {product['id']} повернуто.")

# Обробник кнопки "Правила"
@dp.message_handler(lambda m: m.text == "📖 Правила")
async def show_rules(message: types.Message):
    await message.answer(
        f"📌 Умови користування:\n\n"
        f"* 🧾 Покупець оплачує доставку.\n"
        f"* 💰 Продавець сплачує комісію платформи: 10%\n"
        f"* 💳 Оплата комісії на Monobank: {MONOBANK_CARD_NUMBER}"
    )
    logging.info(f"Користувач {message.from_user.id} переглянув правила.")

# Обробник кнопки "Мої товари"
@dp.message_handler(lambda m: m.text == "📋 Мої товари")
async def my_products(message: types.Message):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM products WHERE user_id = $1 ORDER BY created_at DESC", message.from_user.id)
        if not rows:
            await message.answer("У вас ще немає товарів. Натисніть '📦 Додати товар', щоб створити перше оголошення.")
            logging.info(f"Користувач {message.from_user.id} не має товарів.")
            return

        for row in rows:
            status_emoji = {
                "pending": "⏳",
                "approved": "✅",
                "rejected": "❌",
                "sold": "✅",
                "rotated": "🔄"
            }.get(row['status'], "")

            status_text = {
                "pending": "Очікує модерацію",
                "approved": "Опубліковано",
                "rejected": "Відхилено",
                "sold": "Продано",
                "rotated": "Потребує перевірки фото"
            }.get(row['status'], row['status'])

            product_info = (
                f"📦 {row['name']}\n"
                f"💰 {row['price']}\n"
                f"📅 {row['created_at'].strftime('%d.%m.%Y %H:%M')}\n"
                f"Статус: {status_emoji} {status_text}"
            )

            kb = InlineKeyboardMarkup(row_width=2)
            if row['status'] == "approved":
                if row['channel_message_id']:
                    kb.add(InlineKeyboardButton("👁 Переглянути в каналі", url=f"https://t.me/c/{str(CHANNEL_ID)[4:]}/{row['channel_message_id']}"))
                kb.add(InlineKeyboardButton("✅ Продано", callback_data=f"sold:{row['id']}"))
                kb.add(InlineKeyboardButton("✏ Змінити ціну", callback_data=f"editprice:{row['id']}"))
                kb.add(InlineKeyboardButton("🗑 Видалити", callback_data=f"delete:{row['id']}"))
            elif row['status'] == "pending":
                kb.add(InlineKeyboardButton("⌛ Очікує модерацію", callback_data="noop"))
                kb.add(InlineKeyboardButton("✏ Змінити ціну", callback_data=f"editprice:{row['id']}"))
                kb.add(InlineKeyboardButton("🗑 Видалити", callback_data=f"delete:{row['id']}"))
            elif row['status'] == "rotated":
                kb.add(InlineKeyboardButton("🔁 Переопублікувати", callback_data=f"repost:{row['id']}"))
                kb.add(InlineKeyboardButton("✏ Змінити ціну", callback_data=f"editprice:{row['id']}"))
                kb.add(InlineKeyboardButton("🗑 Видалити", callback_data=f"delete:{row['id']}"))
            elif row['status'] == "sold":
                kb.add(InlineKeyboardButton("✅ Продано", callback_data="noop"))
                kb.add(InlineKeyboardButton("🗑 Видалити", callback_data=f"delete:{row['id']}"))
            elif row['status'] == "rejected":
                kb.add(InlineKeyboardButton("❌ Відхилено", callback_data="noop"))
                kb.add(InlineKeyboardButton("✏ Змінити ціну", callback_data=f"editprice:{row['id']}"))
                kb.add(InlineKeyboardButton("🗑 Видалити", callback_data=f"delete:{row['id']}"))

            await message.answer(product_info, reply_markup=kb)
            logging.info(f"Користувач {message.from_user.id} переглянув товар {row['id']}.")

# Обробник кнопки "Продано"
@dp.callback_query_handler(lambda c: c.data.startswith("sold:"))
async def mark_sold(callback: types.CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM products WHERE id = $1 AND user_id = $2", product_id, callback.from_user.id)
        if not row:
            await callback.answer("Товар не знайдено або у вас немає прав.", show_alert=True)
            return

        if row['status'] == 'sold':
            await callback.answer("Цей товар вже позначено як проданий.", show_alert=True)
            return

        price_text = row['price']
        commission = 0
        try:
            # Витягуємо тільки цифри з ціни
            price_digits = ''.join(filter(str.isdigit, price_text))
            if price_digits:
                price = int(price_digits)
                commission = max(10, round(price * 0.1)) # Мінімум 10 грн комісії
            else:
                commission = 200 # Якщо ціна не містить цифр (наприклад, "договірна")
        except ValueError:
            commission = 200 # Якщо не вдалося перетворити ціну на число

        await conn.execute("UPDATE products SET status = 'sold' WHERE id = $1", product_id)

    await bot.send_message(callback.from_user.id,
        f"💸 Комісія 10% = {commission} грн\n💳 Оплатіть на картку Monobank: {MONOBANK_CARD_NUMBER}")
    await callback.answer("Товар позначено як проданий.")
    await callback.message.edit_reply_markup(reply_markup=None) # Видаляємо кнопки після дії
    logging.info(f"Товар {product_id} позначено як проданий користувачем {callback.from_user.id}.")

# Обробник кнопки "Видалити"
@dp.callback_query_handler(lambda c: c.data.startswith("delete:"))
async def delete_product(callback: types.CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM products WHERE id = $1 AND user_id = $2", product_id, callback.from_user.id)
    await callback.message.edit_text("🗑 Товар видалено.")
    await callback.answer("Товар видалено.")
    logging.info(f"Товар {product_id} видалено користувачем {callback.from_user.id}.")

# Обробник кнопки "Змінити ціну"
@dp.callback_query_handler(lambda c: c.data.startswith("editprice:"))
async def edit_price_prompt(callback: types.CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split(":")[1])
    # Перевіряємо, чи належить товар користувачу
    async with db_pool.acquire() as conn:
        product = await conn.fetchrow("SELECT id FROM products WHERE id = $1 AND user_id = $2", product_id, callback.from_user.id)
        if not product:
            await callback.answer("Товар не знайдено або у вас немає прав.", show_alert=True)
            return

    await state.update_data(editing_product_id=product_id)
    await callback.message.answer("✏ Введіть нову ціну (грн, USD або договірна):")
    await EditPrice.NewPrice.set()
    await callback.answer("Готуємося до зміни ціни.")
    logging.info(f"Користувач {callback.from_user.id} ініціював зміну ціни для товару {product_id}.")

# Застосування нової ціни
@dp.message_handler(state=EditPrice.NewPrice)
async def apply_new_price(message: types.Message, state: FSMContext):
    data = await state.get_data()
    product_id = data.get('editing_product_id')
    if not product_id:
        await message.answer("Виникла помилка. Спробуйте ще раз.")
        await state.finish()
        return

    new_price = message.text
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE products SET price = $1 WHERE id = $2 AND user_id = $3", new_price, product_id, message.from_user.id)
    await message.answer("💰 Ціну оновлено!")
    await state.finish()
    logging.info(f"Користувач {message.from_user.id} оновив ціну для товару {product_id} на {new_price}.")

# Обробник кнопки "Переопублікувати"
@dp.callback_query_handler(lambda c: c.data.startswith("repost:"))
async def repost_product(callback: types.CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    async with db_pool.acquire() as conn:
        product = await conn.fetchrow("SELECT repost_count, status FROM products WHERE id = $1 AND user_id = $2", product_id, callback.from_user.id)
        if not product:
            await callback.answer("Товар не знайдено або у вас немає прав.", show_alert=True)
            return

        if product['repost_count'] >= 3:
            await callback.answer("Ви досягли ліміту переопублікацій (3 рази).", show_alert=True)
            return
        if product['status'] == 'pending':
            await callback.answer("Товар вже очікує модерацію.", show_alert=True)
            return

        await conn.execute("UPDATE products SET status = 'pending', repost_count = repost_count + 1 WHERE id = $1 AND user_id = $2", product_id, callback.from_user.id)
    await callback.answer("Надіслано повторно на модерацію")
    await callback.message.edit_text("🔁 Товар повторно подано на модерацію.")
    logging.info(f"Товар {product_id} переопубліковано користувачем {callback.from_user.id}.")

    # Повідомлення модераторам про повторну модерацію
    async with db_pool.acquire() as conn:
        reposted_product = await conn.fetchrow("SELECT * FROM products WHERE id = $1", product_id)
    if reposted_product:
        moderator_product_info = (
            f"🔄 Товар повторно на модерацію!\n\n"
            f"📦 Назва: {reposted_product['name']}\n"
            f"💰 Ціна: {reposted_product['price']}\n"
            f"📝 Опис: {reposted_product['description']}\n"
            f"📍 Локація: {reposted_product['location']}\n"
            f"🚚 Доставка: {reposted_product['delivery']}\n"
            f"👤 Продавець: @{reposted_product['username']} (ID: {reposted_product['user_id']})"
        )
        moderator_keyboard = InlineKeyboardMarkup(row_width=2)
        moderator_keyboard.add(
            InlineKeyboardButton("✅ Опублікувати", callback_data=f"approve:{reposted_product['user_id']}:{reposted_product['name']}"),
            InlineKeyboardButton("❌ Відхилити", callback_data=f"reject:{reposted_product['user_id']}:{reposted_product['name']}")
        )
        moderator_keyboard.add(
            InlineKeyboardButton("🔄 Повернути фото", callback_data=f"rotate:{reposted_product['user_id']}:{reposted_product['name']}")
        )

        media_group = [InputMediaPhoto(file_id) for file_id in reposted_product['photos']]
        if media_group:
            try:
                await bot.send_media_group(ADMIN_IDS[0], media_group)
            except Exception as e:
                logging.error(f"Помилка при відправці медіагрупи повторно модератору: {e}")
                await bot.send_message(ADMIN_IDS[0], "Помилка при завантаженні фотографій для повторної модерації. Будь ласка, перевірте вручну.")

        for admin_id in ADMIN_IDS:
            await bot.send_message(admin_id, moderator_product_info, reply_markup=moderator_keyboard)
            logging.info(f"Модератору {admin_id} надіслано повідомлення про повторну модерацію товару {product_id}.")

# Обробник для "noop" callback (для кнопок без дії)
@dp.callback_query_handler(lambda c: c.data == "noop")
async def no_operation(callback: types.CallbackQuery):
    await callback.answer("Ця кнопка не має дії.")

# Обробник команди /stats (для користувача)
@dp.message_handler(commands=["stats"])
async def show_stats(message: types.Message):
    async with db_pool.acquire() as conn:
        count_all = await conn.fetchval("SELECT COUNT(*) FROM products WHERE user_id = $1", message.from_user.id)
        count_pending = await conn.fetchval("SELECT COUNT(*) FROM products WHERE user_id = $1 AND status = 'pending'", message.from_user.id)
        count_approved = await conn.fetchval("SELECT COUNT(*) FROM products WHERE user_id = $1 AND status = 'approved'", message.from_user.id)
        count_sold = await conn.fetchval("SELECT COUNT(*) FROM products WHERE user_id = $1 AND status = 'sold'", message.from_user.id)
        count_rejected = await conn.fetchval("SELECT COUNT(*) FROM products WHERE user_id = $1 AND status = 'rejected'", message.from_user.id)
        count_rotated = await conn.fetchval("SELECT COUNT(*) FROM products WHERE user_id = $1 AND status = 'rotated'", message.from_user.id)

    await message.answer(
        f"Статистика ваших товарів:\n"
        f"Всього: {count_all}\n"
        f"Очікують модерації: {count_pending}\n"
        f"Опубліковано: {count_approved}\n"
        f"Продано: {count_sold}\n"
        f"Відхилено: {count_rejected}\n"
        f"Потребують перевірки фото: {count_rotated}"
    )
    logging.info(f"Користувач {message.from_user.id} запросив статистику.")

# Ініціалізуємо Flask-додаток
app = Flask(__name__)

# URL для Webhook
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
# RENDER_EXTERNAL_URL буде надано Render.com
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL") + WEBHOOK_PATH

@app.route(WEBHOOK_PATH, methods=['POST'])
async def webhook():
    if request.method == 'POST':
        update = types.Update.to_object(request.json)
        # Використовуємо asyncio.create_task для обробки оновлення
        # Це дозволяє Flask швидко повернути відповідь, поки aiogram обробляє оновлення
        asyncio.create_task(dp.process_update(update))
        return 'ok'
    return 'not ok'

@app.route('/')
async def index():
    return 'Bot is running!'

# Функція, яка виконується при запуску додатка
async def on_startup(dispatcher):
    await init_db() # Ініціалізуємо базу даних
    await bot.set_webhook(WEBHOOK_URL) # Встановлюємо Webhook
    logging.info(f"Webhook встановлено на: {WEBHOOK_URL}")

# Функція, яка виконується при завершенні роботи додатка
async def on_shutdown(dispatcher):
    logging.warning('Shutting down..')
    await bot.delete_webhook() # Видаляємо Webhook
    if db_pool:
        await db_pool.close() # Закриваємо пул підключень до БД
    logging.warning('Bye!')

# Запуск бота через Gunicorn (для Render.com)
# Цей блок `if __name__ == "__main__":` не буде виконуватися Gunicorn,
# оскільки Gunicorn імпортує `app` безпосередньо.
# Однак, для локального тестування він буде корисним.
if __name__ == '__main__':
    # Для локального запуску та тестування без Gunicorn
    # У production Render.com запускатиме `gunicorn app:app`
    # і викличе `on_startup` та `on_shutdown` через ASGI/WSGI
    # Для Flask це не відбувається автоматично, тому on_startup
    # викликається перед запуском Flask.
    loop = asyncio.get_event_loop()
    loop.run_until_complete(on_startup(dp))
    # Запускаємо Flask-додаток. Gunicorn зробить це за нас на Render.com.
    # app.run(host='0.0.0.0', port=os.environ.get("PORT", 5000))
    # Для локального тестування:
    # app.run(host='0.0.0.0', port=5000)
    pass # Gunicorn запустить додаток, тому тут не потрібно app.run()

