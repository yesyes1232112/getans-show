import os
import io
import re
import json
import time
import pickle
import random
from io import BytesIO
from threading import Thread
from collections import defaultdict

import requests
from flask import Flask

import telebot
from telebot import types

from bs4 import BeautifulSoup
from PIL import Image
from PyPDF2 import PdfReader

import google.generativeai as genai
import replicate


# -------------------- Config --------------------

SUBSCRIPTION_DAYS = 25

TRIAL_PERIOD_SECONDS = 600          # 10 minutes
TRIAL_COOLDOWN_SECONDS = 5 * 24 * 60 * 60  # 5 days

PORT = int(os.environ.get("PORT", "8080"))
SELF_PING_URL = os.environ.get("SELF_PING_URL")  

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_ID")

GEMINI_API_KEYS_RAW = os.environ.get("GEMINI_API_KEYS", "")
REPLICATE_API_KEYS_RAW = os.environ.get("REPLICATE_API_KEYS", "")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID is not set")
ADMIN_ID = int(ADMIN_ID)

GEMINI_API_KEYS = [k.strip() for k in GEMINI_API_KEYS_RAW.split(",") if k.strip()]
REPLICATE_API_KEYS = [k.strip() for k in REPLICATE_API_KEYS_RAW.split(",") if k.strip()]

if not GEMINI_API_KEYS:
    raise RuntimeError("GEMINI_API_KEYS is not set (comma-separated)")
if not REPLICATE_API_KEYS:
    raise RuntimeError("REPLICATE_API_KEYS is not set (comma-separated)")


# -------------------- Storage files --------------------

SUBSCRIBERS_FILE = "subscribers.json"       # { "user_id": expires_ts }
PENDING_REQUESTS_FILE = "requests.pkl"      # set(str_user_id)
CHECK_PHOTOS_FILE = "check_photos.pkl"      # { str_user_id: bytes }
IMAGE_KEYS_FILE = "image_keys.pkl"          # { str_user_id: { "keys": int } }
TRIALS_FILE = "trials.pkl"                  # { int_user_id: {start_time,last_trial_time,used_image} }
LANG_FILE = "user_languages.pkl"


# -------------------- Flask keep-alive --------------------

app = Flask(__name__)

@app.route("/")
def index():
    return "alive"

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

Thread(target=run_flask, daemon=True).start()

def ping_self():
    if not SELF_PING_URL:
        return
    while True:
        try:
            requests.get(SELF_PING_URL, timeout=10)
        except Exception:
            pass
        time.sleep(300)

Thread(target=ping_self, daemon=True).start()


# -------------------- Bot + Gemini init --------------------

bot = telebot.TeleBot(BOT_TOKEN)

current_key_index = 0
genai.configure(api_key=GEMINI_API_KEYS[current_key_index]) #5 keys
model = genai.GenerativeModel("gemini-2.5-flash")


# -------------------- Helpers: persistence --------------------

def safe_load_pickle(file_path: str, default_value):
    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        try:
            with open(file_path, "rb") as f:
                return pickle.load(f)
        except (EOFError, pickle.UnpicklingError):
            return default_value
    return default_value

def safe_save_pickle(file_path: str, obj) -> None:
    with open(file_path, "wb") as f:
        pickle.dump(obj, f)

def load_json(file_path: str, default_value):
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default_value
    return default_value

def save_json(file_path: str, obj) -> None:
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


# -------------------- Subscribers / requests / checks --------------------

subscribers = load_json(SUBSCRIBERS_FILE, {})  

pending_requests = safe_load_pickle(PENDING_REQUESTS_FILE, set())  
pending_check_photos = safe_load_pickle(CHECK_PHOTOS_FILE, {})     

image_keys = safe_load_pickle(IMAGE_KEYS_FILE, {})  

def save_subscribers():
    save_json(SUBSCRIBERS_FILE, subscribers)

def save_pending_requests():
    safe_save_pickle(PENDING_REQUESTS_FILE, pending_requests)

def save_pending_check_photos():
    safe_save_pickle(CHECK_PHOTOS_FILE, pending_check_photos)

def save_image_keys():
    safe_save_pickle(IMAGE_KEYS_FILE, image_keys)


# -------------------- Trial (no database) --------------------

trials = safe_load_pickle(TRIALS_FILE, {})  

# normalize keys if older versions stored them as strings
try:
    trials = {int(k): v for k, v in trials.items()}
except Exception:
    trials = {}

def save_trials():
    safe_save_pickle(TRIALS_FILE, trials)

def _get_trial_record(user_id: int):
    return trials.get(int(user_id))

def _upsert_trial_record(user_id: int, record: dict):
    trials[int(user_id)] = record
    save_trials()

def _set_trial_used_image(user_id: int, used: int):
    rec = _get_trial_record(user_id) or {}
    rec["used_image"] = int(used)
    rec.setdefault("start_time", None)
    rec.setdefault("last_trial_time", None)
    _upsert_trial_record(user_id, rec)

def start_trial(user_id: int):
    now = time.time()
    _upsert_trial_record(user_id, {
        "start_time": now,
        "last_trial_time": now,
        "used_image": 0,
    })

def get_trial_info(user_id: int):
    rec = _get_trial_record(user_id)
    if rec:
        return rec.get("start_time"), rec.get("last_trial_time")
    return None, None

def is_trial_active(user_id: int) -> bool:
    start_time, _ = get_trial_info(user_id)
    return bool(start_time and (time.time() - start_time) < TRIAL_PERIOD_SECONDS)

def can_start_trial(user_id: int) -> bool:
    _, last_trial_time = get_trial_info(user_id)
    if not last_trial_time:
        return True
    return (time.time() - last_trial_time) >= TRIAL_COOLDOWN_SECONDS

def get_trial_time(user_id: int):
    rec = _get_trial_record(user_id)
    if not rec:
        return None
    return rec.get("start_time")


# -------------------- Access checks --------------------

def is_subscribed(user_id) -> bool:
    uid = str(user_id)
    return uid in subscribers and float(subscribers[uid]) > time.time()

def has_active_subscription(user_id) -> bool:
    return is_subscribed(user_id)


# -------------------- Languages --------------------

TRANSLATIONS = {
    "ru": {
        "start_greeting": "<b>🔥Привет!🔥</b>\n\n- Напиши /subscribe для доступа.\n- /trial для пробного доступа.\n- /profile для просмотра профиля.\n- /help для помощи.\n- /language для смены языка.",
        "access_active": "✅ Доступ активен! Отправьте вопрос.",
        "trial_active": "🕒 Пробный доступ активен. Отправьте вопрос.",
        "no_access": "🔒 Нет доступа. Напиши /subscribe или /trial.",
        "admin_only": "⛔ Только для администратора.",
        "no_subscribers": "📭 Нет активных подписчиков.",
        "subscription_active": "✅ Подписка уже активна!\n\nМожешь поддержать проект ☺:\n💳 4449 9451 0094 0896 (🇦🇿Akart)",
        "subscription_info": "Подписка 1.9₼ на 25 дней.\nОплати на карту:\n\n💳 4449 9451 0094 0896 (🇦🇿Akart)\n\n📸 Отправь скрин чека.",
        "trial_activated": "🎉 Пробный период активирован на {} минут!",
        "trial_cooldown": "⏳ Пробный доступ можно получить раз в 5 дней.\nОсталось подождать: {} минут.",
        "generating_image": "🖼 Генерирую изображение, подождите...",
        "reading_pdf": "📄 Читаю PDF...",
        "language_selected": "🌍 Выбран русский язык",
        "deletion": "❌ Ваша подписка была отключена администратором.",
        "processing": "✍ Обрабатываю длинный текст, подождите немного...",
        "analyzing_link": "🔍 Анализирую ссылку...",
        "only1": "🚫 Во время пробного периода можно сгенерировать изображение только 1 раз.",
        "txt": "📄 Читаю файл и отвечаю на вопросы...",
        "pdf_error": "❌ Не удалось прочитать текст из PDF. Убедись, что это не скан.",
        "this_is_task": "📝 Это задание",
        "this_is_receipt": "🧾 Это чек",
        "choose_image_type": "Выберите тип изображения:",
        "photo_not_found": "❌ Фото не найдено. Пожалуйста, отправьте заново.",
        "receipt_received": "✅ Чек получен. Ожидайте одобрения.",
        "receipt_already_sent": "⚠ Вы уже отправляли чек. Ожидайте одобрения.",
        "trial_given": "🎉 Вам выдан пробный доступ на {minutes} минут!",
        "trial_status": "🕒 Пробный период активен. Осталось: {seconds} сек.",
        "profile_active": "👤 Ваш профиль:\n\n📅 Подписка активна. Осталось: {days} дн.\n🔑 Ключи для генерации: {keys}",
        "profile_inactive": "👤 Ваш профиль:\n\n❌ Подписка не активна.\n🔑 Доступные ключи (заморожены): {keys}\n\nЧтобы использовать ключи — оформите подписку.",
        "subscription_activated": "✅ Подписка активирована на {days} дней!",
        "no_keys": "❌ У вас закончились ключи для генерации изображения. Дождитесь новой подписки.",
        "select_language": "🌍 Выберите язык:",
        "help_text": """
<b>    🛠 Доступные команды:</b>
/start — Начать работу с ботом
/subscribe — Инструкция по оплате подписки
/status — Узнать статус подписки
/trial - Получить пробный доступ
/language — Сменить язык интерфейса
/help — Показать список всех команд

<b>📌 Как пользоваться ботом:</b>
— Отправь вопрос текстом 📝 - получишь ответ 
— Отправь ссылку 🔗 - бот проанализирует и решит тест
— Отправь фото задания или скриншот 📷 - бот распознает и решит
— Отправь описание изображения 🌅 - получишь сгенерированное изображение  

ℹ Чтобы сгенерировать изображение, используй ключевые слова как: <i>сгенерируй &lt;твоё описание&gt;</i> или <i>generate &lt;your description&gt;</i>  

<b>    🔒 Подписка:</b>
Без подписки бот не работает. Используй /subscribe чтобы получить доступ.

🆘 <b><a href="https://t.me/aarzmnl">Поддержка</a></b>
"""
    },
    "en": {
        "start_greeting": "<b>🔥Hello!🔥</b>\n\n- Write /subscribe for access.\n- /trial for trial access.\n- /profile to check the profile.\n- /help for help.\n- /language to change language.",
        "access_active": "✅ Access is active! Send your question.",
        "trial_active": "🕒 Trial access is active. Send your question.",
        "no_access": "🔒 No access. Write /subscribe or /trial.",
        "admin_only": "⛔ Admin only.",
        "no_subscribers": "📭 No active subscribers.",
        "subscription_active": "✅ Subscription is already active!\n\nYou can support the project ☺:\n💳 4449 9451 0094 0896 (🇦🇿Akart)",
        "subscription_info": "Subscription 1.9₼ for 25 days.\nPay to card:\n\n💳 4449 9451 0094 0896 (🇦🇿Akart)\n\n📸 Send screenshot of receipt.",
        "trial_activated": "🎉 Trial period activated for {} minutes!",
        "trial_cooldown": "⏳ Trial access can be obtained once every 5 days.\nTime left to wait: {} minutes.",
        "generating_image": "🖼 Generating image, please wait...",
        "reading_pdf": "📄 Reading PDF...",
        "language_selected": "🌍 English language selected",
        "deletion": "❌ Your subscription has been disabled by the administrator.",
        "processing": "✍ Processing long text, please wait a bit...",
        "analyzing_link": "🔍 Analyzing the link...",
        "only1": "🚫 During the trial period, you can generate an image only once.",
        "txt": "📄 Reading the file and answering the questions...",
        "pdf_error": "❌ Unable to read text from PDF. Make sure it is not a scan.",
        "this_is_task": "📝 This is a task",
        "this_is_receipt": "🧾 This is a receipt",
        "choose_image_type": "Choose image type:",
        "photo_not_found": "❌ Photo not found. Please resend.",
        "receipt_received": "✅ Receipt received. Wait for approval.",
        "receipt_already_sent": "⚠ You have already sent a receipt. Wait for approval.",
        "trial_given": "🎉 You have been given trial access for {minutes} minutes!",
        "trial_status": "🕒 Trial period is active. Time left: {seconds} sec.",
        "no_keys": "❌ You have run out of image generation keys. Please wait for a new subscription.",
        "profile_active": "👤 Your profile:\n\n📅 Subscription active. Remaining: {days} days\n🔑 Image generation keys: {keys}",
        "profile_inactive": "👤 Your profile:\n\n❌ Subscription not active.\n🔑 Available keys (frozen): {keys}\n\nSubscribe to use keys.",
        "subscription_activated": "✅ Subscription activated for {days} days!",
        "select_language": "🌍 Choose language:",
        "help_text": """
<b>    🛠 Available commands:</b>
/start — Start working with the bot
/subscribe — Payment subscription instructions
/status — Check subscription status
/trial - Get trial access
/language — Change interface language
/help — Show list of all commands

<b>📌 How to use the bot:</b>
— Send text question 📝 - get answer 
— Send link 🔗 - bot will analyze and solve test
— Send photo of task or screenshot 📷 - bot will recognize and solve
— Send image description 🌅 - get generated image  

ℹ To generate image, use keywords like: <i>generate &lt;your description&gt;</i>  

<b>    🔒 Subscription:</b>
Bot doesn't work without subscription. Use /subscribe to get access.

🆘 <b><a href="https://t.me/aarzmnl">Support</a></b>
"""
    },
    "az": {
        "start_greeting": "<b>🔥Salam!🔥</b>\n\n- Giriş üçün /subscribe.\n- Sınaq girişi üçün /trial.\n- Profil üçün /profile.\n- Kömək üçün /help.\n- Dili dəyişmək üçün /language.",
        "access_active": "✅ Giriş aktivdir! Sualınızı göndərin.",
        "trial_active": "🕒 Sınaq girişi aktivdir. Sualınızı göndərin.",
        "no_access": "🔒 Giriş yoxdur. /subscribe və ya /trial",
        "admin_only": "⛔ Yalnız administrator üçün.",
        "no_subscribers": "📭 Aktiv abunəçi yoxdur.",
        "subscription_active": "✅ Abunəlik artıq aktivdir!\n\nLayihəni dəstəkləyə bilərsiniz ☺:\n💳 4449 9451 0094 0896 (🇦🇿Akart)",
        "subscription_info": "Abunəlik 1.9₼ 25 gün üçün.\nKartla ödəyin:\n\n💳 4449 9451 0094 0896 (🇦🇿Akart)\n\n📸 Qəbzi şəkil kimi göndərin.",
        "trial_activated": "🎉 Sınaq müddəti {} dəqiqə üçün aktivləşdirildi!",
        "trial_cooldown": "⏳ Sınaq girişi 5 gündə bir dəfə alına bilər.\nGözləmə vaxtı: {} dəqiqə.",
        "generating_image": "🖼 Şəkil yaradılır, gözləyin...",
        "reading_pdf": "📄 PDF oxunur...",
        "language_selected": "🌍 Azərbaycan dili seçildi",
        "deletion": "❌ Abunəliyiniz administrator tərəfindən deaktiv edilib.",
        "processing": "✍ Uzun mətn emal olunur, bir az gözləyin...",
        "analyzing_link": "🔍 Link təhlil edilir...",
        "only1": "🚫 Sınaq müddətində yalnız bir dəfə şəkil yarada bilərsiniz.",
        "txt": "📄 Faylı oxuyuram və suallara cavab verirəm...",
        "pdf_error": "❌ PDF-dən mətni oxumaq mümkün deyil. Bunun skan olmadığına əmin olun.",
        "this_is_task": "📝 Bu bir tapşırığdır",
        "this_is_receipt": "🧾 Bu bir çekdir",
        "choose_image_type": "Şəkil növünü seçin:",
        "photo_not_found": "❌ Şəkil tapılmadı. Zəhmət olmasa yenidən göndərin.",
        "receipt_received": "✅ Qəbz alındı. Təsdiqi gözləyin.",
        "receipt_already_sent": "⚠ Artıq qəbz göndərmisiniz. Təsdiqi gözləyin.",
        "trial_given": "🎉 Sizə {minutes} dəqiqə sınaq girişi verildi!",
        "no_keys": "❌ Şəkil yaratmaq üçün açarlarınız qurtardı. Yeni abunəliyi gözləyin.",
        "profile_active": "👤 Profiliniz:\n\n📅 Abunəlik aktivdir. Qalan: {days} gün\n🔑 Şəkil yaratmaq açarları: {keys}",
        "profile_inactive": "👤 Profiliniz:\n\n❌ Abunəlik aktiv deyil.\n🔑 Mövcud açarlar (dondurulub): {keys}\n\nAçarları istifadə etmək üçün abunə olun.",
        "trial_status": "🕒 Sınaq dövrü aktivdir. Qalan: {seconds} san.",
        "subscription_activated": "✅ Abunəlik {days} günlük aktivləşdirildi!",
        "select_language": "🌍 Dil seçin:",
        "help_text": """
<b>    🛠 Mövcud əmrlər:</b>
/start — Bot ilə işə başlayın
/subscribe — Abunəlik ödəniş təlimatları
/status — Abunəlik statusunu yoxlayın
/trial - Sınaq girişi əldə edin
/language — İnterfeys dilini dəyişin
/help — Bütün əmrlərin siyahısını göstərin

<b>📌 Botdan necə istifadə etmək olar:</b>
— Mətn sualı göndərin 📝 - cavab alın 
— Link göndərin 🔗 - bot təhlil edib testi həll edəcək
— Tapşırığın şəklini göndərin 📷 - bot tanıyıb həll edəcək
— Şəkil təsvirini göndərin 🌅 - yaradılmış şəkil alın  

ℹ Şəkil yaratmaq üçün bu açar sözləri istifadə edin: <i>generate &lt;sizin təsviriniz&gt;</i>  

<b>    🔒 Abunəlik:</b>
Bot abunəlik olmadan işləmir. Giriş üçün /subscribe istifadə edin.

🆘 <b><a href="https://t.me/aarzmnl">Dəstək</a></b>
"""
    },
}

user_languages = safe_load_pickle(LANG_FILE, {})

def save_user_languages():
    safe_save_pickle(LANG_FILE, user_languages)

def get_user_language(user_id: int) -> str:
    return user_languages.get(str(user_id), "en")

def get_text(user_id: int, key: str) -> str:
    lang = get_user_language(user_id)
    return TRANSLATIONS.get(lang, TRANSLATIONS["en"]).get(key, TRANSLATIONS["en"].get(key, key))


# -------------------- Text splitting --------------------

def split_text(text: str, max_length: int = 4000):
    parts = []
    while len(text) > max_length:
        idx = text.rfind("\n", 0, max_length)
        if idx == -1:
            idx = max_length
        parts.append(text[:idx].strip())
        text = text[idx:].strip()
    if text:
        parts.append(text)
    return parts


# -------------------- Gemini helpers --------------------

def switch_to_next_key() -> bool:
    global current_key_index
    current_key_index += 1
    if current_key_index >= len(GEMINI_API_KEYS):
        return False
    genai.configure(api_key=GEMINI_API_KEYS[current_key_index])
    return True

def safe_generate_content(prompt_or_parts):
    global model
    retries = 0
    while retries < len(GEMINI_API_KEYS):
        try:
            return model.generate_content(prompt_or_parts)
        except Exception as e:
            msg = str(e).lower()
            if "429" in msg or "quota" in msg:
                if not switch_to_next_key():
                    raise Exception("All Gemini keys are rate-limited.")
                model = genai.GenerativeModel("gemini-2.5-flash")
                retries += 1
            else:
                raise


# -------------------- PDF text extraction --------------------

def extract_text_chunks_from_pdf(pdf_bytes: bytes, pages_per_chunk: int = 5, max_pages: int = 50):
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        pages = reader.pages[:max_pages]
        chunks = []

        for i in range(0, len(pages), pages_per_chunk):
            chunk_pages = pages[i:i + pages_per_chunk]
            chunk_text = ""

            for j, page in enumerate(chunk_pages):
                try:
                    text = page.extract_text() or ""
                    chunk_text += f"\n--- Page {i + j + 1} ---\n{text.strip()}"
                except Exception:
                    continue

            if len(chunk_text.strip().split()) >= 15:
                chunks.append(chunk_text.strip())

        return chunks if chunks else None
    except Exception:
        return None


# -------------------- Replicate image generation --------------------

def set_random_replicate_key():
    key = random.choice(REPLICATE_API_KEYS)
    os.environ["REPLICATE_API_TOKEN"] = key

def generate_image_from_prompt(prompt: str, delay: int = 10):
    try:
        set_random_replicate_key()

        output = replicate.run(
            "recraft-ai/recraft-v3",
            input={
                "prompt": prompt,
                "width": 512,
                "height": 512,
                "num_inference_steps": 30,
                "guidance_scale": 7.5,
                "num_outputs": 1,
            },
        )

        time.sleep(delay)

        if hasattr(output, "url"):
            return output.url
        return None
    except Exception:
        return None


# -------------------- Photos: task vs receipt --------------------

pending_photos = defaultdict(dict)  

def process_image_as_task(user_id: int, file_bytes: bytes, caption: str | None):
    try:
        image = Image.open(BytesIO(file_bytes)).resize((512, 512))
        buffered = BytesIO()
        image.save(buffered, format="PNG")
        img_bytes = buffered.getvalue()

        prompt = caption or "Look at the image and answer in the same language as the task on the photo."

        response = model.generate_content(
            contents=[
                {
                    "parts": [
                        {"inline_data": {"mime_type": "image/png", "data": img_bytes}},
                        {"text": prompt},
                    ]
                }
            ]
        )

        bot.send_message(user_id, response.text.strip() if hasattr(response, "text") else "Gemini error")
    except Exception as e:
        bot.send_message(user_id, f"Failed to process image: {e}")


# -------------------- Language commands --------------------

@bot.message_handler(commands=["language"])
def language_cmd(message):
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(
        types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
        types.InlineKeyboardButton("🇺🇸 English", callback_data="lang_en"),
    )
    keyboard.add(types.InlineKeyboardButton("🇦🇿 Azərbaycan", callback_data="lang_az"))

    bot.send_message(
        message.chat.id,
        get_text(message.from_user.id, "select_language"),
        reply_markup=keyboard,
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("lang_"))
def handle_language_selection(call):
    user_id = str(call.from_user.id)
    lang = call.data.split("_", 1)[1]

    user_languages[user_id] = lang
    save_user_languages()

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=get_text(call.from_user.id, "language_selected"),
    )


# -------------------- Admin: show subscribers / requests --------------------

@bot.message_handler(commands=["subscribers"])
def show_subscribers(message):
    if message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, get_text(message.from_user.id, "admin_only"))
        return

    active = [(uid, exp) for uid, exp in subscribers.items() if float(exp) > time.time()]
    if not active:
        bot.send_message(message.chat.id, get_text(message.from_user.id, "no_subscribers"))
        return

    for uid, expires in active:
        left_days = int((float(expires) - time.time()) / 86400)
        text = f"👤 ID: {uid}\n📅 Days left: {left_days}"

        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton(text="❌ Delete", callback_data=f"delete_{uid}"))
        bot.send_message(message.chat.id, text, reply_markup=keyboard)

@bot.message_handler(commands=["requests"])
def show_requests(message):
    if message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, get_text(message.from_user.id, "admin_only"))
        return

    if not pending_requests:
        bot.send_message(message.chat.id, "📭 No pending requests.")
        return

    for uid in list(pending_requests):
        text = f"👤 Request from ID: {uid}"
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton("✅ Approve", callback_data=f"approve_{uid}"),
            types.InlineKeyboardButton("❌ Reject", callback_data=f"reject_{uid}"),
        )

        if uid in pending_check_photos:
            try:
                bot.send_photo(
                    message.chat.id,
                    photo=io.BytesIO(pending_check_photos[uid]),
                    caption=text,
                    reply_markup=keyboard,
                )
            except Exception:
                bot.send_message(message.chat.id, text + "\n(Receipt photo could not be sent.)", reply_markup=keyboard)
        else:
            bot.send_message(message.chat.id, text, reply_markup=keyboard)


# -------------------- Start / help --------------------

@bot.message_handler(commands=["start"])
def start_cmd(message):
    user_id = message.from_user.id

    if has_active_subscription(user_id):
        bot.send_message(user_id, get_text(user_id, "access_active"))
        return
    if is_trial_active(user_id):
        bot.send_message(user_id, get_text(user_id, "trial_active"))
        return

    try:
        bot.send_photo(
            chat_id=user_id,
            photo="https://raw.githubusercontent.com/yesyes1232112/foto1/c0805efc7d067cff5999787f14994fd748a8dfb0/5788719564379502503.jpg",
            caption=get_text(user_id, "start_greeting"),
            parse_mode="HTML",
        )
    except Exception:
        bot.send_message(user_id, get_text(user_id, "start_greeting"), parse_mode="HTML")

@bot.message_handler(commands=["help"])
def send_help(message):
    try:
        bot.send_animation(
            chat_id=message.chat.id,
            animation="https://raw.githubusercontent.com/yesyes1232112/foto1/refs/heads/main/undefined%20-%20Imgur.gif",
            caption=get_text(message.from_user.id, "help_text"),
            parse_mode="HTML",
        )
    except Exception:
        bot.send_message(
            message.chat.id,
            get_text(message.from_user.id, "help_text"),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


# -------------------- Subscribe / trial / status / profile --------------------

awaiting_payment_proof = set()  # kept for compatibility (manual checks)

@bot.message_handler(commands=["subscribe"])
def subscribe_cmd(message):
    uid = str(message.from_user.id)

    if is_subscribed(uid):
        bot.send_message(message.chat.id, get_text(message.from_user.id, "subscription_active"))
        return

    awaiting_payment_proof.add(int(uid))
    bot.send_message(message.chat.id, get_text(message.from_user.id, "subscription_info"))

@bot.message_handler(commands=["trial"])
def trial_cmd(message):
    user_id = message.from_user.id

    if has_active_subscription(user_id):
        bot.send_message(message.chat.id, get_text(user_id, "subscription_active"))
        return

    if is_trial_active(user_id):
        left = int(TRIAL_PERIOD_SECONDS - (time.time() - get_trial_time(user_id)))
        bot.send_message(message.chat.id, get_text(user_id, "trial_status").format(seconds=left))
        return

    if not can_start_trial(user_id):
        _, last_trial_time = get_trial_info(user_id)
        cooldown_left = int((TRIAL_COOLDOWN_SECONDS - (time.time() - last_trial_time)) / 60)
        bot.send_message(message.chat.id, get_text(user_id, "trial_cooldown").format(cooldown_left))
        return

    start_trial(user_id)
    awaiting_payment_proof.discard(user_id)
    bot.send_message(message.chat.id, get_text(user_id, "trial_activated").format(TRIAL_PERIOD_SECONDS // 60))

@bot.message_handler(commands=["status"])
def status_cmd(message):
    uid = str(message.from_user.id)

    if is_subscribed(uid):
        left = int((float(subscribers[uid]) - time.time()) / 86400)
        bot.send_message(message.chat.id, f"<b>✅ Subscription active</b>. Remaining: {left} days.", parse_mode="HTML")
        return

    if is_trial_active(int(uid)):
        left = int(TRIAL_PERIOD_SECONDS - (time.time() - get_trial_time(int(uid))))
        bot.send_message(message.chat.id, f"🕒 Trial period is active. Remaining: {left} sec.")
        return

    bot.send_message(message.chat.id, get_text(message.from_user.id, "no_access"))

@bot.message_handler(commands=["profile"])
def profile_cmd(message):
    uid = str(message.from_user.id)

    keys = int(image_keys.get(uid, {}).get("keys", 0))

    if has_active_subscription(uid):
        left_days = int((float(subscribers[uid]) - time.time()) / 86400)
        bot.send_message(message.chat.id, get_text(message.from_user.id, "profile_active").format(days=left_days, keys=keys))
    else:
        bot.send_message(message.chat.id, get_text(message.from_user.id, "profile_inactive").format(keys=keys))


# -------------------- Admin: give subscription / give trial --------------------

@bot.message_handler(commands=["givesub"])
def give_subscription_cmd(message):
    if message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, get_text(message.from_user.id, "admin_only"))
        return

    try:
        parts = message.text.split()
        if len(parts) != 3:
            raise ValueError("Bad command format")

        user_id = str(int(parts[1]))
        days = int(parts[2])

        subscribers[user_id] = time.time() + days * 86400
        save_subscribers()

        image_keys[user_id] = {"keys": 10}
        save_image_keys()

        bot.send_message(int(user_id), get_text(int(user_id), "subscription_activated").format(days=days))
        bot.send_message(message.chat.id, f"✅ Subscription added for {user_id} ({days} days).")
    except Exception:
        bot.send_message(message.chat.id, "❌ Usage: /givesub <user_id> <days>")

@bot.message_handler(commands=["trialgive"])
def give_trial_cmd(message):
    if message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, get_text(message.from_user.id, "admin_only"))
        return

    try:
        _, user_id = message.text.split()
        user_id = int(user_id)

        start_trial(user_id)
        bot.send_message(user_id, get_text(user_id, "trial_given").format(minutes=TRIAL_PERIOD_SECONDS // 60))
        bot.send_message(message.chat.id, f"✅ Trial given to {user_id}.")
    except Exception:
        bot.send_message(message.chat.id, "❌ Usage: /trialgive <user_id>")


# -------------------- Documents: PDF / TXT --------------------

@bot.message_handler(content_types=["document"])
def handle_document(message):
    user_id = message.from_user.id

    if not (has_active_subscription(user_id) or is_trial_active(user_id)):
        bot.send_message(message.chat.id, get_text(user_id, "no_access"))
        return

    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)

        # PDF
        if message.document.mime_type == "application/pdf" or message.document.file_name.lower().endswith(".pdf"):
            bot.send_message(user_id, get_text(user_id, "reading_pdf"))
            chunks = extract_text_chunks_from_pdf(downloaded)

            if not chunks:
                bot.send_message(user_id, get_text(user_id, "pdf_error"))
                return

            for idx, chunk in enumerate(chunks):
                prompt = (
                    "Answer ALL questions in this PDF part in the same language as the PDF. "
                    "Pick one correct option per question. Be short. "
                    "At the end add: AI can make mistakes.\n\n"
                    f"{chunk}"
                )

                bot.send_message(user_id, f"📤 Sending part {idx + 1} of {len(chunks)}...")
                response = safe_generate_content(prompt)
                answer = response.text.strip() if hasattr(response, "text") else "Gemini error"
                for part in split_text(answer, 4000):
                    bot.send_message(user_id, part)
            return

        # TXT
        if message.document.file_name.lower().endswith(".txt"):
            bot.send_message(message.chat.id, get_text(user_id, "txt"))
            text = downloaded.decode("utf-8", errors="ignore")

            prompt = (
                "Answer all questions from the text below. "
                "Answer briefly and in the same language as the text.\n\n"
                f"{text}"
            )
            response = safe_generate_content(prompt)
            result = response.text.strip() if hasattr(response, "text") else "Gemini error"

            for part in split_text(result, 4000):
                bot.send_message(message.chat.id, part)
            return

        bot.send_message(message.chat.id, "⚠ Only .pdf and .txt files are supported.")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ File error: {e}")


# -------------------- Photos --------------------

@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    user_id = message.from_user.id

    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded = bot.download_file(file_info.file_path)

        # If subscribed -> treat as task
        if has_active_subscription(user_id):
            process_image_as_task(user_id, downloaded, message.caption)
            return

        # Otherwise ask: task or receipt
        pending_photos[user_id] = {"file": downloaded, "caption": message.caption}

        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton(get_text(user_id, "this_is_task"), callback_data="img_task"),
            types.InlineKeyboardButton(get_text(user_id, "this_is_receipt"), callback_data="img_receipt"),
        )

        bot.send_message(message.chat.id, get_text(user_id, "choose_image_type"), reply_markup=keyboard)

    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Image upload error: {e}")

@bot.callback_query_handler(func=lambda call: call.data in ["img_task", "img_receipt"])
def handle_image_decision(call):
    user_id = call.from_user.id

    photo_data = pending_photos.pop(user_id, None)
    if not photo_data or "file" not in photo_data:
        bot.send_message(user_id, get_text(user_id, "photo_not_found"))
        return

    if call.data == "img_task":
        if not (has_active_subscription(user_id) or is_trial_active(user_id)):
            bot.send_message(user_id, get_text(user_id, "no_access"))
            return

        process_image_as_task(user_id, photo_data["file"], photo_data.get("caption"))
        return

    # img_receipt
    if str(user_id) in pending_requests:
        bot.send_message(user_id, get_text(user_id, "receipt_already_sent"))
        return

    pending_check_photos[str(user_id)] = photo_data["file"]
    save_pending_check_photos()

    pending_requests.add(str(user_id))
    save_pending_requests()

    caption = f"👤 New receipt from user ID: {user_id}"
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(
        types.InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user_id}"),
        types.InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user_id}"),
    )

    try:
        bot.send_photo(ADMIN_ID, photo=BytesIO(photo_data["file"]), caption=caption, reply_markup=keyboard)
    except Exception:
        bot.send_message(ADMIN_ID, f"⚠ Could not send receipt photo from user {user_id}")

    bot.send_message(user_id, get_text(user_id, "receipt_received"))


# -------------------- Links parsing --------------------

def get_full_visible_text(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        )
    }
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    for tag in soup(["script", "style"]):
        tag.extract()

    for hidden in soup.select('[style*="display:none"], [style*="visibility:hidden"]'):
        hidden.extract()

    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)

def process_link(url: str) -> str:
    try:
        full_text = get_full_visible_text(url)
        prompt = (
            "Answer ALL questions from the test. Pick only ONE correct option for each question. "
            "List answers in order with the question number and a short explanation in the same language as the questions. "
            "At the end write: AI can make mistakes."
        )
        response = safe_generate_content(prompt + "\n\nTEXT:\n" + full_text)
        return response.text if hasattr(response, "text") else "Gemini error"
    except Exception as e:
        return f"Link error: {e}"


# -------------------- Admin callbacks: delete / approve / reject --------------------

@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_"))
def delete_subscriber(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Admin only.")
        return

    uid = call.data.split("_", 1)[1]

    if uid in subscribers:
        del subscribers[uid]
        save_subscribers()

        try:
            bot.send_message(int(uid), get_text(int(uid), "deletion"))
        except Exception:
            pass

        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"✅ Subscriber {uid} removed.",
        )
    else:
        bot.answer_callback_query(call.id, "Already removed.")

def process_rejection_reason(message, uid: str, original_message):
    reason = message.text.strip()
    try:
        bot.send_message(int(uid), f"❌ Your request has been rejected.\nReason: {reason}")
        bot.edit_message_caption(
            chat_id=original_message.chat.id,
            message_id=original_message.message_id,
            caption=f"❌ Request from user ID: {uid} rejected.\nReason: {reason}",
            reply_markup=None,
        )
    except Exception:
        pass
    finally:
        pending_requests.discard(uid)
        save_pending_requests()

@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_") or call.data.startswith("reject_"))
def handle_request_decision(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Admin only.")
        return

    action, uid = call.data.split("_", 1)
    uid = str(uid)

    if uid not in pending_requests:
        bot.answer_callback_query(call.id, "Already processed.")
        return

    if action == "approve":
        subscribers[uid] = time.time() + SUBSCRIPTION_DAYS * 86400
        save_subscribers()

        image_keys[uid] = {"keys": 10}
        save_image_keys()

        bot.send_message(int(uid), f"✅ Approved! Subscription is active for {SUBSCRIPTION_DAYS} days.")
        new_caption = f"✅ Request from user ID: {uid} approved."
    else:
        msg = bot.send_message(call.message.chat.id, "📝 Write rejection reason:")
        bot.register_next_step_handler(msg, lambda msg2: process_rejection_reason(msg2, uid, call.message))
        return

    pending_requests.remove(uid)
    save_pending_requests()

    try:
        bot.edit_message_caption(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            caption=new_caption,
            reply_markup=None,
        )
    except Exception:
        pass


# -------------------- Announcements (admin) --------------------

announcement_mode = {}

@bot.message_handler(commands=["announce"])
def announce_cmd(message):
    if message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, get_text(message.from_user.id, "admin_only"))
        return

    active_subscribers = sum(1 for _, exp in subscribers.items() if float(exp) > time.time())
    if active_subscribers == 0:
        bot.send_message(message.chat.id, "📭 No active subscribers.")
        return

    announcement_mode[ADMIN_ID] = True
    bot.send_message(
        message.chat.id,
        f"📢 Announcement mode enabled!\n"
        f"👥 Active subscribers: {active_subscribers}\n\n"
        f"Send the announcement text (or /cancel)."
    )

@bot.message_handler(commands=["cancel"])
def cancel_announce_cmd(message):
    if message.from_user.id == ADMIN_ID and ADMIN_ID in announcement_mode:
        announcement_mode.pop(ADMIN_ID, None)
        bot.send_message(message.chat.id, "❌ Announcement cancelled.")

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and ADMIN_ID in announcement_mode and not m.text.startswith("/"))
def handle_announcement_text(message):
    announcement_text = message.text

    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(
        types.InlineKeyboardButton("✅ Send", callback_data="send_announcement"),
        types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_announcement"),
    )

    bot.send_message(
        message.chat.id,
        f"📝 Preview:\n\n{announcement_text}\n\nSend to all active subscribers?",
        reply_markup=keyboard,
    )

@bot.callback_query_handler(func=lambda call: call.data in ["send_announcement", "cancel_announcement"])
def handle_announcement_decision(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Admin only.")
        return

    if call.data == "cancel_announcement":
        announcement_mode.pop(ADMIN_ID, None)
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text="❌ Cancelled.")
        return

    # send_announcement
    try:
        announcement_text = call.message.text.split("📝 Preview:\n\n", 1)[1].rsplit("\n\nSend to all active subscribers?", 1)[0]
    except Exception:
        announcement_text = ""

    sent_count = 0
    failed_count = 0

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="📤 Sending...",
    )

    for uid, exp in list(subscribers.items()):
        if float(exp) > time.time():
            try:
                bot.send_message(int(uid), f"📢\n\n{announcement_text}", parse_mode="HTML")
                sent_count += 1
                time.sleep(0.1)
            except Exception:
                failed_count += 1

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=(
            "✅ Done.\n\n"
            f"Sent: {sent_count}\n"
            f"Failed: {failed_count}"
        ),
    )

    announcement_mode.pop(ADMIN_ID, None)


# -------------------- Main text handler --------------------

@bot.message_handler(content_types=["text"])
def handle_text(message):
    text_lower = (message.text or "").lower()

    # trial: only one image generation
    if is_trial_active(message.from_user.id):
        rec = _get_trial_record(message.from_user.id)
        if rec and int(rec.get("used_image", 0)) == 1:
            if any(p in text_lower for p in ["сгенерируй", "generate", "draw", "нарисуй"]):
                bot.send_message(message.chat.id, get_text(message.from_user.id, "only1"))
                return

    # image generation
    if any(p in text_lower for p in ["сгенерируй", "generate image", "generate", "нарисуй", "создай изображение", "создай фото", "создай картинку", "draw"]):
        uid = str(message.from_user.id)

        if not (has_active_subscription(uid) or is_trial_active(message.from_user.id)):
            bot.send_message(message.chat.id, get_text(message.from_user.id, "no_access"))
            return

        if uid not in image_keys or int(image_keys[uid].get("keys", 0)) <= 0:
            bot.send_message(message.chat.id, get_text(message.from_user.id, "no_keys"))
            return

        image_keys[uid]["keys"] = int(image_keys[uid].get("keys", 0)) - 1
        save_image_keys()

        bot.send_message(message.chat.id, get_text(message.from_user.id, "generating_image"))

        url = generate_image_from_prompt(message.text)
        if url:
            try:
                bot.send_photo(message.chat.id, photo=url)
                if is_trial_active(message.from_user.id):
                    _set_trial_used_image(message.from_user.id, 1)
            except Exception:
                bot.send_message(message.chat.id, f"🖼 Image link:\n{url}")
        else:
            bot.send_message(message.chat.id, "❌ Failed to generate image.")
        return

    # ignore commands
    if (message.text or "").startswith("/"):
        return

    user_id = message.from_user.id
    if not (has_active_subscription(user_id) or is_trial_active(user_id)):
        bot.send_message(message.chat.id, get_text(message.from_user.id, "no_access"))
        return

    # link
    m = re.search(r"https?://\S+", message.text or "")
    if m:
        url = m.group()
        bot.send_message(message.chat.id, get_text(message.from_user.id, "analyzing_link"))
        result = process_link(url)
        for chunk in split_text(result, 4096):
            bot.send_message(message.chat.id, chunk)
        return

    if len(message.text or "") > 10000:
        bot.send_message(message.chat.id, get_text(message.from_user.id, "processing"))

    prompt = "Answer in the same language as the user's message.\n\n" + (message.text or "")
    response = safe_generate_content(prompt)
    result = response.text.strip() if hasattr(response, "text") else "Gemini error"

    for part in split_text(result, 4000):
        bot.send_message(message.chat.id, part)


# -------------------- Runner --------------------

def start_bot():
    bot.remove_webhook()
    while True:
        try:
            bot.polling(non_stop=True)
        except Exception:
            time.sleep(5)

time.sleep(1)
start_bot()












