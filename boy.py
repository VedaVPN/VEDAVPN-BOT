import os
import time
from io import BytesIO

import psycopg2
import psycopg2.pool
import qrcode
import telebot
from telebot import types
from flask import Flask, request, abort
from github import Github

TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_ID = int(os.environ.get('ADMIN_ID'))
CHANNEL = '@vedavpn'
FORUM_LINK_DEFAULT = 'https://t.me/vedavpnforum'
VPN_LINK_DEFAULT = "https://pastebin.com/raw/քո_նոր_հղումը_այստեղ"

# GitHub-ում պահվող sub ֆայլի կարգավորումներ (VPN բաժանորդագրության հղումի աղբյուր)
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')
GITHUB_REPO = os.environ.get('GITHUB_REPO', 'VedaVPN/VEDAVPN-BOT')
GITHUB_SUB_PATH = os.environ.get('GITHUB_SUB_PATH', 'sub')


def get_sub_file_contents():
    """Օգնական ֆունկցիա՝ sub ֆայլի ընթացիկ contents օբյեկտը GitHub-ից բերելու համար։"""
    g = Github(GITHUB_TOKEN)
    repo = g.get_repo(GITHUB_REPO)
    return repo, repo.get_contents(GITHUB_SUB_PATH)

# Supabase PostgreSQL connection string (Project Settings → Database → Connection string → URI)
SUPABASE_URL = os.environ.get(
    'SUPABASE_URL',
    'postgresql://postgres:YOUR-PASSWORD@db.your-project-ref.supabase.co:5432/postgres'
)

# The Render URL that Telegram will send webhook requests to.
# Render automatically sets the RENDER_EXTERNAL_URL environment variable for every Web Service,
# so you usually don't need to set this manually.
WEBHOOK_HOST = os.environ.get('RENDER_EXTERNAL_URL', 'https://your-app-name.onrender.com')
WEBHOOK_PATH = '/webhook'
WEBHOOK_URL = WEBHOOK_HOST + WEBHOOK_PATH

STORE_LINKS = {
    'happ': {
        'android': 'https://play.google.com/store/apps/details?id=com.happproxy',
        'ios': 'https://apps.apple.com/us/app/happ-proxy-utility/id6504287215',
    },
    'incy': {
        'android': 'https://play.google.com/store/apps/details?id=llc.itdev.incy',
        'ios': 'https://apps.apple.com/ru/app/incy/id6756943388',
    },
}

bot = telebot.TeleBot(TOKEN, parse_mode="HTML", threaded=False)
app = Flask(__name__)


# === DATABASE (PostgreSQL/Supabase via a reusable connection pool) ===
# Opening a brand-new TCP/TLS connection for every single query (as before)
# is what was making replies feel slow -- one button tap could trigger a
# dozen+ separate connections. A pool keeps a handful of connections open
# and reuses them, so a query is just a quick round trip instead of a full
# handshake every time.
db_pool = psycopg2.pool.ThreadedConnectionPool(1, 10, SUPABASE_URL)


def db_execute(query, params=(), fetchone=False, fetchall=False, commit=False):
    conn = db_pool.getconn()
    try:
        cur = conn.cursor()
        cur.execute(query, params)
        if commit:
            conn.commit()
        if fetchone:
            return cur.fetchone()
        if fetchall:
            return cur.fetchall()
        return None
    except Exception:
        conn.rollback()
        raise
    finally:
        db_pool.putconn(conn)


def init_db():
    db_execute('''CREATE TABLE IF NOT EXISTS users
                (user_id BIGINT PRIMARY KEY,
                 lang TEXT DEFAULT 'ru',
                 ref_by BIGINT DEFAULT 0,
                 ref_count INTEGER DEFAULT 0)''', commit=True)
    # For older databases, add the device column (to remember the
    # Android/iPhone choice). IF NOT EXISTS handles the case where
    # the column already exists.
    db_execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS device TEXT", commit=True)
    # Bilingual texts (buttons, FAQ, howto, etc.)
    db_execute('''CREATE TABLE IF NOT EXISTS content
                (key TEXT, lang TEXT, value TEXT, PRIMARY KEY (key, lang))''', commit=True)
    # Non-language settings (VPN link, forum link)
    db_execute('''CREATE TABLE IF NOT EXISTS config
                (key TEXT PRIMARY KEY, value TEXT)''', commit=True)
    # Brand-new buttons added by the admin
    db_execute('''CREATE TABLE IF NOT EXISTS custom_buttons
                (id SERIAL PRIMARY KEY,
                 label_hy TEXT, label_ru TEXT,
                 response_hy TEXT, response_ru TEXT)''', commit=True)


# === CONTENT (bilingual texts, editable by the admin) ===
CONTENT_DEFAULTS = {
    'btn_get_vpn':   {'hy': "🛡 Ստանալ VPN", 'ru': "🛡 Получить VPN"},
    'btn_referrals': {'hy': "👥 Ռեֆերալներ", 'ru': "👥 Рефералы"},
    'btn_howto':     {'hy': "📖 Ինչպես տեղադրել", 'ru': "📖 Как установить"},
    'btn_faq':       {'hy': "❓ Հաճախ տրվող հարցեր", 'ru': "❓ Часто задаваемые вопросы"},
    'btn_support':   {'hy': "🆘 Աջակցություն", 'ru': "🆘 Поддержка"},
    'btn_forum':     {'hy': "💬 Ֆորում", 'ru': "💬 Чат (ФОРУМ)"},
    'btn_iptv':      {'hy': "📺 IPTV", 'ru': "📺 IPTV"},

    'text_choose_lang': {
        'hy': "🌍 Ընտրեք լեզուն / Выберите язык:",
        'ru': "🌍 Ընտրեք լեզուն / Выберите язык:",
    },
    'text_subscribe_warn': {
        'hy': "⚠️ Խնդրում ենք բաժանորդագրվել ալիքին և սեղմել ստուգելու կոճակը:",
        'ru': "⚠️ Пожалуйста, подпишитесь на канал и нажмите кнопку проверки:",
    },
    'text_not_subscribed': {
        'hy': "❌ Դուք դեռ բաժանորդագրված չեք:",
        'ru': "❌ Вы еще не подписались:",
    },
    'text_vpn_caption': {
        'hy': "🔗 <b>VPN հղում:</b>\n<code>{link}</code>\n\nℹ️ Չգիտե՞ք որտեղ տեղադրել այս հղումը։ Սեղմեք «📖 Ինչպես տեղադրել» կոճակը menu-ից։",
        'ru': "🔗 <b>Ссылка на VPN:</b>\n<code>{link}</code>\n\nℹ️ Не знаете куда вставить эту ссылку? Нажмите «📖 Как установить» в меню.",
    },
    'text_howto': {
        'hy': (
            "📖 <b>Ինչպես ավելացնել VPN հղումը</b>\n\n"
            "1️⃣ Նախ պատճենեք (copy) VPN հղումը՝ հպելով դրա վրա բոտի հաղորդագրության մեջ\n\n"
            "<b>📱 Happ հավելվածում.</b>\n"
            "• Բացեք Happ հավելվածը\n"
            "• Սեղմեք վերևի աջ «+» կոճակը\n"
            "• Ընտրեք «Add from Clipboard» կամ «Import from URL»\n"
            "• Հաստատեք (հղումն արդեն պատճենած է)\n"
            "• Ընտրեք սերվեր ցանկից և սեղմեք Connect\n\n"
            "<b>📱 INCY հավելվածում.</b>\n"
            "• Բացեք INCY հավելվածը\n"
            "• Սեղմեք «+» կոճակը\n"
            "• Ընտրեք «Paste from Clipboard» կամ «Add Subscription»\n"
            "• Հաստատեք հղումը\n"
            "• Ընտրեք սերվեր և միացեք"
        ),
        'ru': (
            "📖 <b>Как добавить VPN ссылку</b>\n\n"
            "1️⃣ Сначала скопируйте VPN ссылку, нажав на неё в сообщении бота\n\n"
            "<b>📱 В приложении Happ:</b>\n"
            "• Откройте Happ\n"
            "• Нажмите «+» вверху справа\n"
            "• Выберите «Add from Clipboard» или «Import from URL»\n"
            "• Подтвердите (ссылка уже в буфере обмена)\n"
            "• Выберите сервер из списка и нажмите Connect\n\n"
            "<b>📱 В приложении INCY:</b>\n"
            "• Откройте INCY\n"
            "• Нажмите «+»\n"
            "• Выберите «Paste from Clipboard» или «Add Subscription»\n"
            "• Подтвердите ссылку\n"
            "• Выберите сервер и подключитесь"
        ),
    },
    'text_faq': {
        'hy': (
            "❓ <b>Հաճախ տրվող հարցեր</b>\n\n"
            "<b>1. Ինչպե՞ս ստանալ VPN հղումը։</b>\n"
            "Սեղմեք «🛡 Ստանալ VPN», բաժանորդագրվեք ալիքին, և հղումը կուղարկվի ավտոմատ։\n\n"
            "<b>2. Ինչու՞ է պահանջվում բաժանորդագրություն ալիքին։</b>\n"
            "VPN-ը անվճար է տրամադրվում ալիքի բաժանորդներին որպես bonus։\n\n"
            "<b>3. VPN-ը վճարովի՞ է։</b>\n"
            "Ներկայումս ամբողջությամբ անվճար է։\n\n"
            "<b>4. Ինչպե՞ս տեղադրել հղումը հավելվածում։</b>\n"
            "Սեղմեք «📖 Ինչպես տեղադրել» menu-ից՝ քայլ առ քայլ ցուցումների համար։\n\n"
            "<b>5. VPN-ը չի աշխատում, ի՞նչ անել։</b>\n"
            "Փորձեք ընտրել այլ սերվեր հավելվածում։ Եթե չի օգնում, գրեք «🆘 Աջակցություն»-ին։\n\n"
            "<b>6. Ինչպե՞ս ստանալ ավելի շատ bonus/ֆիչրներ։</b>\n"
            "Հրավիրեք ընկերների ձեր ռեֆերալ հղումով («👥 Ռեֆերալներ» բաժնում)։"
        ),
        'ru': (
            "❓ <b>Часто задаваемые вопросы</b>\n\n"
            "<b>1. Как получить VPN ссылку?</b>\n"
            "Нажмите «🛡 Получить VPN», подпишитесь на канал — ссылка придёт автоматически.\n\n"
            "<b>2. Почему нужна подписка на канал?</b>\n"
            "VPN предоставляется бесплатно подписчикам канала в качестве бонуса.\n\n"
            "<b>3. VPN платный?</b>\n"
            "На данный момент полностью бесплатный.\n\n"
            "<b>4. Как вставить ссылку в приложение?</b>\n"
            "Нажмите «📖 Как установить» в меню — там пошаговая инструкция.\n\n"
            "<b>5. VPN не работает, что делать?</b>\n"
            "Попробуйте выбрать другой сервер в приложении. Если не помогает — напишите в «🆘 Поддержка».\n\n"
            "<b>6. Как получить больше бонусов/функций?</b>\n"
            "Приглашайте друзей по вашей реферальной ссылке (раздел «👥 Рефералы»)."
        ),
    },
    'text_support_prompt': {
        'hy': "📩 Գրեք Ձեր հարցը՝",
        'ru': "📩 Напишите ваш вопрос:",
    },
    'text_welcome': {
        'hy': "👋 Բարի գալուստ VedaVPN! Անվճար ու անվտանգ VPN մեր ալիքի բաժանորդների համար։",
        'ru': "👋 Добро пожаловать в VedaVPN! Бесплатный и безопасный VPN для подписчиков нашего канала.",
    },
    'text_iptv_caption': {
        'hy': "📺 <b>IPTV հղում:</b>\n<code>{link}</code>",
        'ru': "📺 <b>Ссылка IPTV:</b>\n<code>{link}</code>",
    },
    'text_iptv_missing': {
        'hy': "❌ IPTV հղումը դեռ սահմանված չէ։",
        'ru': "❌ Ссылка IPTV пока не задана.",
    },
    'text_iptv_instructions': {
        'hy': (
            "\n\nℹ️ <b>Ինչպես միացնել.</b>\n"
            "1️⃣ Պատճենեք (copy) վերևի հղումը\n"
            "2️⃣ Բացեք ձեր IPTV հավելվածը (օր. IPTV Smarters, TiviMate)\n"
            "3️⃣ Ընտրեք «Add Playlist» / «Add from URL»\n"
            "4️⃣ Տեղադրեք հղումը և հաստատեք\n"
            "5️⃣ Սպասեք ալիքների բեռնմանը և վայելեք"
        ),
        'ru': (
            "\n\nℹ️ <b>Как подключить.</b>\n"
            "1️⃣ Скопируйте ссылку выше\n"
            "2️⃣ Откройте ваше IPTV приложение (напр. IPTV Smarters, TiviMate)\n"
            "3️⃣ Выберите «Add Playlist» / «Add from URL»\n"
            "4️⃣ Вставьте ссылку и подтвердите\n"
            "5️⃣ Дождитесь загрузки каналов и наслаждайтесь"
        ),
    },
    'text_unsub_warning': {
        'hy': (
            "⚠️ <b>Դուք դուրս եկաք ալիքից</b>\n\n"
            "VPN ծառայությունը հասանելի է միայն մեր ալիքի բաժանորդներին։ "
            "Եթե ցանկանում եք շարունակել օգտվել VPN-ից, խնդրում ենք նորից բաժանորդագրվել։"
        ),
        'ru': (
            "⚠️ <b>Вы отписались от канала</b>\n\n"
            "VPN доступен только подписчикам нашего канала. "
            "Если хотите продолжать пользоваться VPN, пожалуйста, подпишитесь снова."
        ),
    },
    'btn_device_android': {'hy': "🤖 Android", 'ru': "🤖 Android"},
    'btn_device_ios':     {'hy': "🍏 iPhone", 'ru': "🍏 iPhone"},
    'btn_open_happ':      {'hy': "🚀 Բացել Happ-ում", 'ru': "🚀 Открыть в Happ"},
    'btn_open_incy':      {'hy': "🚀 Բացել INCY-ում", 'ru': "🚀 Открыть в INCY"},
    'btn_store_google':   {'hy': "⬇️ Google Play", 'ru': "⬇️ Google Play"},
    'btn_store_apple':    {'hy': "⬇️ App Store", 'ru': "⬇️ App Store"},
}


def get_content(key, lang):
    row = db_execute("SELECT value FROM content WHERE key = %s AND lang = %s", (key, lang), fetchone=True)
    if row:
        return row[0]
    return CONTENT_DEFAULTS.get(key, {}).get(lang, f"[{key}/{lang}]")


def set_content(key, lang, value):
    db_execute(
        "INSERT INTO content (key, lang, value) VALUES (%s, %s, %s) "
        "ON CONFLICT (key, lang) DO UPDATE SET value = EXCLUDED.value",
        (key, lang, value), commit=True
    )


# === CONFIG (non-language settings, e.g. the VPN link) ===
CONFIG_DEFAULTS = {
    'vpn_link': VPN_LINK_DEFAULT,
    'forum_link': FORUM_LINK_DEFAULT,
    'iptv_link': "",
    'welcome_photo': "",
}


def get_config(key):
    row = db_execute("SELECT value FROM config WHERE key = %s", (key,), fetchone=True)
    if row:
        return row[0]
    return CONFIG_DEFAULTS.get(key, "")


def set_config(key, value):
    db_execute(
        "INSERT INTO config (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, value), commit=True
    )


def check_sub(user_id):
    try:
        return bot.get_chat_member(CHANNEL, user_id).status in ['member', 'administrator', 'creator']
    except Exception:
        return False


def get_lang(user_id):
    row = db_execute("SELECT lang FROM users WHERE user_id = %s", (user_id,), fetchone=True)
    return row[0] if row else 'ru'


def get_device(user_id):
    row = db_execute("SELECT device FROM users WHERE user_id = %s", (user_id,), fetchone=True)
    return row[0] if row and row[0] else None


def set_device(user_id, device):
    db_execute("UPDATE users SET device = %s WHERE user_id = %s", (device, user_id), commit=True)


def get_main_menu(lang):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(get_content('btn_get_vpn', lang), get_content('btn_referrals', lang))
    markup.add(get_content('btn_howto', lang), get_content('btn_faq', lang))
    markup.add(get_content('btn_support', lang), get_content('btn_forum', lang))
    markup.add(get_content('btn_iptv', lang))

    custom = db_execute("SELECT label_hy, label_ru FROM custom_buttons", fetchall=True) or []
    for label_hy, label_ru in custom:
        markup.add(label_hy if lang == 'hy' else label_ru)

    return markup


def generate_qr(data):
    qr = qrcode.QRCode(box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = BytesIO()
    bio.name = "vpn_qr.png"
    img.save(bio, 'PNG')
    bio.seek(0)
    return bio


def build_app_markup(user_id, lang):
    """Builds the Android/iPhone switch + store button."""
    device = get_device(user_id) or 'android'
    markup = types.InlineKeyboardMarkup(row_width=2)

    android_label = ("✅ " if device == 'android' else "") + get_content('btn_device_android', lang)
    ios_label = ("✅ " if device == 'ios' else "") + get_content('btn_device_ios', lang)
    markup.add(
        types.InlineKeyboardButton(android_label, callback_data="device_android"),
        types.InlineKeyboardButton(ios_label, callback_data="device_ios"),
    )

    store_label = get_content('btn_store_apple', lang) if device == 'ios' else get_content('btn_store_google', lang)
    markup.add(types.InlineKeyboardButton(store_label, url=STORE_LINKS['incy'][device]))

    return markup


def build_info_markup(lang):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton(get_content("btn_terms", lang), callback_data="info_terms"),
        types.InlineKeyboardButton(get_content("btn_privacy", lang), callback_data="info_privacy"),
    )
    return markup

def send_vpn_link(chat_id, lang):
    link = get_config('vpn_link')
    caption = get_content('text_vpn_caption', lang).format(link=link)
    qr_bio = generate_qr(link)
    bot.send_photo(chat_id, qr_bio, caption=caption, reply_markup=build_app_markup(chat_id, lang))


@bot.callback_query_handler(func=lambda call: call.data in ("device_android", "device_ios"))
def switch_device(call):
    device = call.data.split('_')[1]
    set_device(call.from_user.id, device)
    lang = get_lang(call.from_user.id)
    try:
        bot.edit_message_reply_markup(
            call.message.chat.id, call.message.message_id,
            reply_markup=build_app_markup(call.from_user.id, lang)
        )
    except Exception:
        pass
    bot.answer_callback_query(call.id)


# === START ===
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.chat.id
    args = message.text.split()
    ref_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0

    db_execute(
        "INSERT INTO users (user_id, ref_by) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
        (user_id, ref_id), commit=True
    )
    if ref_id > 0 and ref_id != user_id:
        db_execute("UPDATE users SET ref_count = ref_count + 1 WHERE user_id = %s", (ref_id,), commit=True)

    # Remove the old menu buttons shown below, until the language is chosen
    welcome_photo = get_config('welcome_photo')
    welcome_caption = get_content('text_welcome', 'ru')
    if welcome_photo:
        try:
            bot.send_photo(user_id, welcome_photo, caption=welcome_caption, reply_markup=types.ReplyKeyboardRemove())
        except Exception:
            bot.send_message(user_id, welcome_caption, reply_markup=types.ReplyKeyboardRemove())
    else:
        bot.send_message(user_id, welcome_caption, reply_markup=types.ReplyKeyboardRemove())

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🇦🇲 Հայերեն", callback_data="lang_hy"),
               types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"))
    bot.send_message(user_id, "👇", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith('lang_'))
def set_lang(call):
    lang = call.data.split('_')[1]
    db_execute("UPDATE users SET lang = %s WHERE user_id = %s", (lang, call.from_user.id), commit=True)
    bot.send_message(call.from_user.id, "✅", reply_markup=get_main_menu(lang))


# === VPN CHECK ===
@bot.message_handler(func=lambda m: m.text in (get_content('btn_get_vpn', 'hy'), get_content('btn_get_vpn', 'ru')))
def get_vpn(message):
    lang = get_lang(message.chat.id)
    if not check_sub(message.chat.id):
        markup = types.InlineKeyboardMarkup()
        if lang == 'hy':
            markup.add(types.InlineKeyboardButton("📢 Բաժանորդագրվել", url=f"https://t.me/{CHANNEL.replace('@', '')}"))
            markup.add(types.InlineKeyboardButton("🔄 Ստուգել", callback_data="check_sub"))
        else:
            markup.add(types.InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/{CHANNEL.replace('@', '')}"))
            markup.add(types.InlineKeyboardButton("🔄 Проверить", callback_data="check_sub"))

        bot.send_message(message.chat.id, get_content('text_subscribe_warn', lang), reply_markup=markup)
        return
    send_vpn_link(message.chat.id, lang)


@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def check_sub_callback(call):
    lang = get_lang(call.from_user.id)
    if check_sub(call.from_user.id):
        bot.answer_callback_query(call.id, "✅ OK!")
        send_vpn_link(call.from_user.id, lang)
    else:
        bot.answer_callback_query(call.id, get_content('text_not_subscribed', lang))


# === HOW TO INSTALL ===
@bot.message_handler(func=lambda m: m.text in (get_content('btn_howto', 'hy'), get_content('btn_howto', 'ru')))
def howto(message):
    lang = get_lang(message.chat.id)
    bot.send_message(message.chat.id, get_content('text_howto', lang))


# === FAQ ===
@bot.message_handler(func=lambda m: m.text in (get_content('btn_faq', 'hy'), get_content('btn_faq', 'ru')))
def faq(message):
    lang = get_lang(message.chat.id)
    bot.send_message(message.chat.id, get_content('text_faq', lang))


# === AUTOMATIC UNSUBSCRIBE WARNING ===
@bot.chat_member_handler()
def handle_membership_change(update):
    try:
        channel_username = CHANNEL.lstrip('@')
        if update.chat.username != channel_username:
            return

        old_status = update.old_chat_member.status
        new_status = update.new_chat_member.status
        user_id = update.new_chat_member.user.id

        was_subscribed = old_status in ('member', 'administrator', 'creator')
        now_unsubscribed = new_status in ('left', 'kicked')

        if was_subscribed and now_unsubscribed:
            lang = get_lang(user_id)
            btn_text = "📢 Բաժանորդագրվել կրկին" if lang == 'hy' else "📢 Подписаться снова"
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton(btn_text, url=f"https://t.me/{channel_username}"))
            bot.send_message(user_id, get_content('text_unsub_warning', lang), reply_markup=markup, parse_mode="HTML")
    except Exception:
        pass


# === REFERRALS, FORUM ===
@bot.message_handler(func=lambda m: m.text in (get_content('btn_referrals', 'hy'), get_content('btn_referrals', 'ru')))
def show_refs(message):
    lang = get_lang(message.chat.id)
    row = db_execute("SELECT ref_count FROM users WHERE user_id = %s", (message.chat.id,), fetchone=True)
    count = row[0] if row else 0
    if lang == 'hy':
        text = f"👥 Ձեր հրավիրած օգտատերերը՝ {count}\n🔗 Հղում՝ https://t.me/vedavpn_bot?start={message.chat.id}"
    else:
        text = f"👥 Ваших рефералов: {count}\n🔗 Ссылка: https://t.me/vedavpn_bot?start={message.chat.id}"
    bot.send_message(message.chat.id, text)


@bot.message_handler(func=lambda m: m.text in (get_content('btn_forum', 'hy'), get_content('btn_forum', 'ru')))
def forum(message):
    lang = get_lang(message.chat.id)
    markup = types.InlineKeyboardMarkup()
    btn_text = "🔗 Ֆորում" if lang == 'hy' else "🔗 Форум"
    msg_text = "🔗 Մեր ֆորումը՝" if lang == 'hy' else "🔗 Наш форум:"
    markup.add(types.InlineKeyboardButton(btn_text, url=get_config('forum_link')))
    bot.send_message(message.chat.id, msg_text, reply_markup=markup)


# === IPTV ===
@bot.message_handler(func=lambda m: m.text in (get_content('btn_iptv', 'hy'), get_content('btn_iptv', 'ru')))
def iptv(message):
    lang = get_lang(message.chat.id)
    link = get_config('iptv_link')
    if not link:
        bot.send_message(message.chat.id, get_content('text_iptv_missing', lang))
        return
    caption = get_content('text_iptv_caption', lang).format(link=link)
    instructions = get_content('text_iptv_instructions', lang).strip()
    if instructions:
        caption += "\n\n" + instructions
    bot.send_message(message.chat.id, caption)


# === SUPPORT ===
@bot.message_handler(func=lambda m: m.text in (get_content('btn_support', 'hy'), get_content('btn_support', 'ru')))
def support(message):
    lang = get_lang(message.chat.id)
    bot.send_message(message.chat.id, get_content('text_support_prompt', lang))


# === ADMIN. General stats ===
@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.chat.id != ADMIN_ID:
        return
    row = db_execute("SELECT COUNT(*) FROM users", fetchone=True)
    total = row[0] if row else 0
    bot.send_message(
        ADMIN_ID,
        f"📊 Օգտատերեր: {total}\n\n"
        f"<b>Հրամաններ.</b>\n"
        f"/broadcast տեքստ — ուղարկել բոլորին\n"
        f"/reply ID տեքստ — պատասխանել user-ի\n"
        f"/listkeys — ցույց տալ բոլոր editable content key-ները\n"
        f"/getcontent key — ցույց տալ key-ի ընթացիկ hy/ru արժեքները\n"
        f"/setcontent key lang տեքստ — փոխել կոճակի/տեքստի արժեքը\n"
        f"/getconfig — ցույց տալ VPN/forum հղումները\n"
        f"/setlink նոր_հղում — փոխել VPN հղումը\n"
        f"/setforum նոր_հղում — փոխել forum հղումը\n"
        f"/setiptv նոր_հղում — փոխել IPTV հղումը\n"
        f"/setphoto — սահմանել ողջյունի նկարը (ուղարկիր նկար caption-ում /setphoto, կամ reply արա նկարին)\n\n"
        f"<b>Նոր կոճակներ.</b>\n"
        f"/addbutton անուն_hy|անուն_ru|պատասխան_hy|պատասխան_ru — ավելացնել նոր կոճակ (առանց prefix, պարզապես 4 մաս | -ով)\n"
        f"/listbuttons — ցույց տալ ավելացված կոճակները\n"
        f"/removebutton ID — հեռացնել կոճակ\n\n"
        f"<b>Sub ֆայլի կառավարում (GitHub).</b>\n"
        f"/update_sub [տեքստ] — թարմացնել ամբողջ sub ֆայլը\n"
        f"/append_sub [հղում] — ավելացնել նոր սերվեր (առանց ջնջելու)\n"
        f"/delete_sub_keyword [հիմնաբառ] — ջնջել սերվերը\n"
        f"/list_and_delete — ցույց տալ սերվերները կոճակներով\n"
        f"/show_sub — ցույց տալ ֆայլի բովանդակությունը\n"
        f"/clear_sub — ջնջել բոլոր սերվերները"
    )


@bot.message_handler(commands=['broadcast'])
def broadcast(message):
    if message.chat.id != ADMIN_ID:
        return
    try:
        text = message.text.split(maxsplit=1)[1]
    except IndexError:
        bot.send_message(ADMIN_ID, "❌ Օգտագործիր՝ /broadcast տեքստ")
        return

    users = db_execute("SELECT user_id FROM users", fetchall=True) or []
    sent = 0
    for (uid,) in users:
        try:
            bot.send_message(uid, text)
            sent += 1
        except Exception:
            continue
    bot.send_message(ADMIN_ID, f"✅ Ուղարկված է {sent} օգտատերի")


@bot.message_handler(commands=['reply'])
def reply(message):
    if message.chat.id != ADMIN_ID:
        return
    try:
        parts = message.text.split(maxsplit=2)
        bot.send_message(int(parts[1]), f"📩 Պատասխան ադմինից / Ответ от админа:\n\n{parts[2]}")
        bot.send_message(ADMIN_ID, f"✅ Ուղարկվեց user {parts[1]}-ին")
    except Exception:
        bot.send_message(ADMIN_ID, "❌ Սխալ ID կամ տեքստ. /reply ID տեքստ")


# === ADMIN. CONTENT EDITING (buttons, FAQ, howto) ===
@bot.message_handler(commands=['listkeys'])
def list_keys(message):
    if message.chat.id != ADMIN_ID:
        return
    keys = "\n".join(f"• <code>{k}</code>" for k in CONTENT_DEFAULTS.keys())
    bot.send_message(
        ADMIN_ID,
        f"📋 <b>Editable content key-եր.</b>\n\n{keys}\n\n"
        f"Օգտագործիր՝ /getcontent key — ընթացիկ արժեքը տեսնելու\n"
        f"/setcontent key lang նոր_տեքստ — փոխելու (lang = hy կամ ru)"
    )


@bot.message_handler(commands=['getcontent'])
def get_content_cmd(message):
    if message.chat.id != ADMIN_ID:
        return
    try:
        key = message.text.split(maxsplit=1)[1].strip()
    except IndexError:
        bot.send_message(ADMIN_ID, "❌ Օգտագործիր՝ /getcontent key")
        return
    if key not in CONTENT_DEFAULTS:
        bot.send_message(ADMIN_ID, f"❌ Անհայտ key. Տես /listkeys")
        return
    hy_val = get_content(key, 'hy')
    ru_val = get_content(key, 'ru')
    bot.send_message(
        ADMIN_ID,
        f"🔑 <b>{key}</b>\n\n"
        f"🇦🇲 HY:\n<code>{hy_val}</code>\n\n"
        f"🇷🇺 RU:\n<code>{ru_val}</code>"
    )


@bot.message_handler(commands=['setcontent'])
def set_content_cmd(message):
    if message.chat.id != ADMIN_ID:
        return
    try:
        parts = message.text.split(maxsplit=3)
        key, lang, new_text = parts[1], parts[2], parts[3]
    except IndexError:
        bot.send_message(ADMIN_ID, "❌ Օգտագործիր՝ /setcontent key lang նոր_տեքստ\n(lang = hy կամ ru)")
        return

    if key not in CONTENT_DEFAULTS:
        bot.send_message(ADMIN_ID, f"❌ Անհայտ key «{key}». Տես /listkeys")
        return
    if lang not in ('hy', 'ru'):
        bot.send_message(ADMIN_ID, "❌ lang-ը պետք է լինի hy կամ ru")
        return

    set_content(key, lang, new_text)
    bot.send_message(ADMIN_ID, f"✅ «{key}» ({lang}) թարմացվեց։")


# === ADMIN. CONFIG (VPN link, forum link) ===
@bot.message_handler(commands=['getconfig'])
def get_config_cmd(message):
    if message.chat.id != ADMIN_ID:
        return
    bot.send_message(
        ADMIN_ID,
        f"⚙️ <b>Ընթացիկ config.</b>\n\n"
        f"VPN link:\n<code>{get_config('vpn_link')}</code>\n\n"
        f"Forum link:\n<code>{get_config('forum_link')}</code>\n\n"
        f"IPTV link:\n<code>{get_config('iptv_link') or '❌ սահմանված չէ'}</code>\n\n"
        f"Ողջյունի նկար: {'✅ սահմանված է' if get_config('welcome_photo') else '❌ սահմանված չէ'}"
    )


@bot.message_handler(commands=['setlink'])
def set_link_cmd(message):
    if message.chat.id != ADMIN_ID:
        return
    try:
        new_link = message.text.split(maxsplit=1)[1].strip()
    except IndexError:
        bot.send_message(ADMIN_ID, "❌ Օգտագործիր՝ /setlink նոր_հղում")
        return
    set_config('vpn_link', new_link)
    bot.send_message(ADMIN_ID, f"✅ VPN հղումը թարմացվեց.\n<code>{new_link}</code>")


@bot.message_handler(commands=['setforum'])
def set_forum_cmd(message):
    if message.chat.id != ADMIN_ID:
        return
    try:
        new_link = message.text.split(maxsplit=1)[1].strip()
    except IndexError:
        bot.send_message(ADMIN_ID, "❌ Օգտագործիր՝ /setforum նոր_հղում")
        return
    set_config('forum_link', new_link)
    bot.send_message(ADMIN_ID, f"✅ Forum հղումը թարմացվեց.\n<code>{new_link}</code>")


@bot.message_handler(commands=['setiptv'])
def set_iptv_cmd(message):
    if message.chat.id != ADMIN_ID:
        return
    try:
        new_link = message.text.split(maxsplit=1)[1].strip()
    except IndexError:
        bot.send_message(ADMIN_ID, "❌ Օգտագործիր՝ /setiptv նոր_հղում")
        return
    set_config('iptv_link', new_link)
    bot.send_message(ADMIN_ID, f"✅ IPTV հղումը թարմացվեց.\n<code>{new_link}</code>")


# === ADMIN. WELCOME PHOTO (before /start) ===
def _save_welcome_photo(message):
    photo = None
    if message.photo:
        photo = message.photo[-1].file_id
    elif message.reply_to_message and message.reply_to_message.photo:
        photo = message.reply_to_message.photo[-1].file_id

    if not photo:
        bot.send_message(
            ADMIN_ID,
            "❌ Ուղարկիր նկարը՝ caption-ում գրելով /setphoto,\n"
            "կամ reply արա նկարին /setphoto հրամանով։"
        )
        return

    set_config('welcome_photo', photo)
    bot.send_message(ADMIN_ID, "✅ Ողջյունի նկարը թարմացվեց։ Այն ցուցադրվելու է /start-ից հետո, մինչև լեզվի ընտրությունը։")


@bot.message_handler(commands=['setphoto'])
def set_photo_cmd(message):
    if message.chat.id != ADMIN_ID:
        return
    _save_welcome_photo(message)


@bot.message_handler(
    content_types=['photo'],
    func=lambda m: m.chat.id == ADMIN_ID and m.caption and m.caption.strip().split()[0] == '/setphoto'
)
def set_photo_with_caption(message):
    _save_welcome_photo(message)


# === ADMIN. BRAND-NEW BUTTONS ===
@bot.message_handler(commands=['addbutton'])
def add_button_cmd(message):
    if message.chat.id != ADMIN_ID:
        return
    try:
        content = message.text.split(maxsplit=1)[1]
        parts = content.split('|', 3)
        label_hy, label_ru, resp_hy, resp_ru = [p.strip() for p in parts]
        if not (label_hy and label_ru and resp_hy and resp_ru):
            raise ValueError
    except Exception:
        bot.send_message(
            ADMIN_ID,
            "❌ Ֆորմատ (առանց hy_/ru_ prefix-ների, պարզապես 4 մաս | -ով).\n"
            "<code>/addbutton [կոճակի անունը հայերեն]|[կոճակի անունը ռուսերեն]|[պատասխանը հայերեն]|[պատասխանը ռուսերեն]</code>\n\n"
            "Օրինակ.\n"
            "<code>/addbutton 🔒 Անվտանգություն|🔒 Безопасность|Երբեք մի օգտագործեք VPN-ը հանրային WiFi-ում առանց...|Никогда не используйте VPN в публичном WiFi без...</code>"
        )
        return

    db_execute(
        "INSERT INTO custom_buttons (label_hy, label_ru, response_hy, response_ru) VALUES (%s, %s, %s, %s)",
        (label_hy, label_ru, resp_hy, resp_ru), commit=True
    )
    bot.send_message(ADMIN_ID, f"✅ Կոճակ ավելացվեց՝ «{label_hy}» / «{label_ru}»")


@bot.message_handler(commands=['listbuttons'])
def list_buttons_cmd(message):
    if message.chat.id != ADMIN_ID:
        return
    rows = db_execute("SELECT id, label_hy, label_ru FROM custom_buttons", fetchall=True) or []
    if not rows:
        bot.send_message(ADMIN_ID, "ℹ️ Ավելացված custom կոճակներ չկան։")
        return
    text = "📋 <b>Custom կոճակներ.</b>\n\n" + "\n".join(
        f"<code>{bid}</code>. {hy} / {ru}" for bid, hy, ru in rows
    )
    text += "\n\nՀեռացնելու համար. /removebutton ID"
    bot.send_message(ADMIN_ID, text)


@bot.message_handler(commands=['removebutton'])
def remove_button_cmd(message):
    if message.chat.id != ADMIN_ID:
        return
    try:
        button_id = int(message.text.split(maxsplit=1)[1].strip())
    except Exception:
        bot.send_message(ADMIN_ID, "❌ Օգտագործիր՝ /removebutton ID (տես /listbuttons)")
        return
    db_execute("DELETE FROM custom_buttons WHERE id = %s", (button_id,), commit=True)
    bot.send_message(ADMIN_ID, f"✅ Կոճակ {button_id} հեռացվեց։")


# === ADMIN. SUB ֆայլի կառավարում GitHub-ի միջոցով ===
@bot.message_handler(commands=['update_sub'])
def update_sub_cmd(message):
    if message.chat.id != ADMIN_ID:
        return
    try:
        new_content = message.text.split(maxsplit=1)[1]
    except IndexError:
        bot.send_message(ADMIN_ID, "❌ Օգտագործիր՝ /update_sub [ամբողջ նոր տեքստը]")
        return

    try:
        repo, contents = get_sub_file_contents()
        repo.update_file(
            path=contents.path,
            message="Update sub file via bot",
            content=new_content,
            sha=contents.sha,
        )
        bot.send_message(
            ADMIN_ID,
            f"✅ sub ֆայլը թարմացվեց!\n\nԹարմացված տեքստը (առաջին 200 նիշ).\n{new_content[:200]}..."
        )
    except Exception as e:
        bot.send_message(ADMIN_ID, f"❌ Սխալ: {str(e)}")


@bot.message_handler(commands=['append_sub'])
def append_sub_cmd(message):
    if message.chat.id != ADMIN_ID:
        return
    try:
        new_line = message.text.split(maxsplit=1)[1]
    except IndexError:
        bot.send_message(ADMIN_ID, "❌ Օգտագործիր՝ /append_sub [նոր սերվերի հղումը]")
        return

    try:
        repo, contents = get_sub_file_contents()
        current_content = contents.decoded_content.decode('utf-8')
        if not current_content.endswith('\n'):
            current_content += '\n'
        new_content = current_content + new_line + '\n'
        repo.update_file(
            path=contents.path,
            message="Appended new server via bot",
            content=new_content,
            sha=contents.sha,
        )
        bot.send_message(ADMIN_ID, f"✅ Նոր սերվերն ավելացվեց ֆայլի վերջում:\n\n{new_line[:80]}...")
    except Exception as e:
        bot.send_message(ADMIN_ID, f"❌ Սխալ: {str(e)}")


@bot.message_handler(commands=['delete_sub_keyword'])
def delete_sub_keyword_cmd(message):
    if message.chat.id != ADMIN_ID:
        return
    try:
        keyword = message.text.split(maxsplit=1)[1]
    except IndexError:
        bot.send_message(ADMIN_ID, "❌ Օգտագործիր՝ /delete_sub_keyword [հիմնաբառ]")
        return

    try:
        repo, contents = get_sub_file_contents()
        lines = contents.decoded_content.decode('utf-8').split('\n')
        new_lines = [line for line in lines if keyword not in line]
        if len(new_lines) == len(lines):
            bot.send_message(ADMIN_ID, f"⚠️ '{keyword}' հիմնաբառով սերվեր չի գտնվել:")
            return
        new_content = '\n'.join(new_lines)
        repo.update_file(contents.path, f"Deleted containing: {keyword}", new_content, contents.sha)
        bot.send_message(ADMIN_ID, f"✅ Ջնջվեց '{keyword}' պարունակող տողը:")
    except Exception as e:
        bot.send_message(ADMIN_ID, f"❌ Սխալ: {str(e)}")


def extract_server_name(line):
    """
    Extract meaningful name from configuration line.
    Supports: VLESS, VMESS, SS, SSR, Trojan, etc.
    Returns: (name, protocol_type)
    """
    if not line.strip() or line.startswith('#'):
        return None, None
    
    # Extract name after # for VLESS/VMESS
    if '#' in line and line.startswith(('vless://', 'vmess://', 'ss://', 'trojan://', 'ssr://')):
        try:
            parts = line.split('#')
            if len(parts) > 1:
                name = parts[-1].strip()
                if name:
                    # Get protocol type
                    protocol = line.split('://')[0].upper()
                    return name, protocol
        except:
            pass
    
    # Fallback: use first meaningful part
    if '://' in line:
        try:
            protocol = line.split('://')[0].upper()
            # Try to extract a name or use shortened URL
            if '#' in line:
                name = line.split('#')[-1].strip()
            else:
                name = f"Server"
            return name, protocol
        except:
            pass
    
    return None, None


@bot.message_handler(commands=['list_and_delete'])
def list_and_delete(message):
    if message.chat.id != ADMIN_ID:
        return
    try:
        repo, contents = get_sub_file_contents()
        lines = contents.decoded_content.decode('utf-8').split('\n')
        if not lines:
            bot.send_message(ADMIN_ID, "ℹ️ Ֆայլը դատարկ է:")
            return
        
        markup = types.InlineKeyboardMarkup(row_width=1)
        buttons = []
        config_count = 0
        
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            
            # Skip metadata lines
            if line.startswith('#'):
                continue
            
            # Extract meaningful name
            name, protocol = extract_server_name(line)
            if not name:
                name = "Unknown"
            
            config_count += 1
            # Display: Protocol [#] Name
            display_text = f"❌ [{config_count}] {protocol or 'CONFIG'}: {name[:40]}"
            buttons.append(types.InlineKeyboardButton(display_text, callback_data=f"del_line_{i}"))
        
        if not buttons:
            bot.send_message(ADMIN_ID, "ℹ️ Ջնջելու սերվեր չկա:")
            return
        
        markup.add(*buttons)
        bot.send_message(
            ADMIN_ID, 
            f"👇 Ընտրել սերվերը ջնջելու համար:\n\n💡 <i>Ընդամենը {config_count} հատ configuration</i>", 
            reply_markup=markup,
            parse_mode="HTML"
        )
    except Exception as e:
        bot.send_message(ADMIN_ID, f"❌ Սխալ: {str(e)}")


@bot.callback_query_handler(func=lambda call: call.data.startswith('del_line_'))
def process_delete_line(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Միայն ադմինը կարող է:")
        return
    try:
        line_index = int(call.data.split('_')[2])
        repo, contents = get_sub_file_contents()
        lines = contents.decoded_content.decode('utf-8').split('\n')
        del lines[line_index]
        new_content = '\n'.join(lines)
        repo.update_file(contents.path, f"Deleted line {line_index + 1}", new_content, contents.sha)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"✅ {line_index + 1}-րդ տողը ջնջվեց!",
            reply_markup=None,
        )
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Սխալ: {str(e)}")


@bot.message_handler(commands=['show_sub'])
def show_sub(message):
    if message.chat.id != ADMIN_ID:
        return
    try:
        repo, contents = get_sub_file_contents()
        lines = contents.decoded_content.decode('utf-8').split('\n')
        
        # Separate metadata and actual servers
        metadata = []
        servers = []
        
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            if line.startswith('#'):
                metadata.append(f"{i + 1}. {line}")
            else:
                name, protocol = extract_server_name(line)
                display_name = name if name else line[:50]
                servers.append(f"{i + 1}. [{protocol or 'UNKNOWN'}] {display_name}")
        
        # Format output
        formatted = "📋 <b>METADATA / ԿԱՐԳԱՎՈՐՈՒՄՆԵՐ:</b>\n"
        formatted += "\n".join(metadata) if metadata else "  (없음)"
        formatted += "\n\n🖥️ <b>SERVERS / ՍԵՐՎԵՐՆԵՐ ({} հատ):</b>\n".format(len(servers))
        formatted += "\n".join(servers) if servers else "  (Կա չ)"
        
        # Telegram-ի հաղորդագրության սահմանաչափի պատճառով բաժանում ենք մասերի, եթե երկար է
        chunks = [formatted[i:i + 3500] for i in range(0, len(formatted), 3500)] or ["(դատարկ)"]
        for chunk in chunks:
            bot.send_message(ADMIN_ID, chunk, parse_mode="HTML")
    except Exception as e:
        bot.send_message(ADMIN_ID, f"❌ Սխալ: {str(e)}")


@bot.message_handler(commands=['clear_sub'])
def clear_sub_cmd(message):
    if message.chat.id != ADMIN_ID:
        return
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("✅ Այո, ջնջել բոլորը", callback_data="confirm_clear_sub"),
        types.InlineKeyboardButton("❌ Չեղարկել", callback_data="cancel_clear_sub"),
    )
    bot.send_message(ADMIN_ID, "⚠️ Դուք պատրաստվում եք ջնջել ԲՈԼՈՐ սերվերները։\nՀամոզվա՞ծ եք։", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data in ("confirm_clear_sub", "cancel_clear_sub"))
def process_clear_sub(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Միայն ադմինը կարող է:")
        return
    if call.data == "cancel_clear_sub":
        bot.edit_message_text("❌ Չեղարկվեց։", chat_id=call.message.chat.id, message_id=call.message.message_id)
        bot.answer_callback_query(call.id)
        return
    try:
        repo, contents = get_sub_file_contents()
        lines = contents.decoded_content.decode('utf-8').split('\n')
        new_lines = [line for line in lines if line.startswith('#')]
        new_content = '\n'.join(new_lines)
        repo.update_file(contents.path, "Cleared all servers", new_content, contents.sha)
        bot.edit_message_text(
            "✅ Բոլոր սերվերները ջնջվեցին։\nՄնացին միայն վերնագրերը։\n"
            "Նոր սերվերներ ավելացնելու համար օգտագործիր /update_sub կամ /append_sub",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
        )
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Սխալ: {str(e)}")


@bot.message_handler(func=lambda m: db_execute(
    "SELECT 1 FROM custom_buttons WHERE label_hy = %s OR label_ru = %s", (m.text, m.text), fetchone=True
) is not None)
def custom_button_handler(message):
    lang = get_lang(message.chat.id)
    row = db_execute(
        "SELECT response_hy, response_ru FROM custom_buttons WHERE label_hy = %s OR label_ru = %s",
        (message.text, message.text), fetchone=True
    )
    if row:
        bot.send_message(message.chat.id, row[0] if lang == 'hy' else row[1])


# === ALL OTHER MESSAGES → FORWARDED TO ADMIN, with ID and profile link ===
@bot.message_handler(func=lambda m: True)
def forward_to_admin(message):
    if message.chat.id == ADMIN_ID:
        return
    user = message.from_user
    username = f"@{user.username}" if user.username else "(username չկա)"
    profile_link = f"https://t.me/{user.username}" if user.username else f"tg://user?id={user.id}"

    info = (
        f"✉️ <b>Նոր հաղորդագրություն</b>\n"
        f"👤 <b>ID:</b> <code>{user.id}</code>\n"
        f"👤 <b>Username:</b> {username}\n"
        f"🔗 <a href=\"{profile_link}\">Profile-ը բացել</a>\n\n"
        f"↩️ Պատասխանելու համար. <code>/reply {user.id} տեքստ</code>"
    )
    try:
        bot.send_message(ADMIN_ID, info, parse_mode="HTML", disable_web_page_preview=True)
        bot.forward_message(ADMIN_ID, message.chat.id, message.message_id)
    except Exception:
        pass


# === FLASK WEB APP + WEBHOOK ===
@app.route(WEBHOOK_PATH, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        try:
            bot.process_new_updates([update])
        except Exception:
            import traceback
            print("‼️ ERROR while processing update:")
            traceback.print_exc()
        return '', 200
    else:
        abort(403)


@app.route('/', methods=['GET'])
def index():
    # Render's health check hits this root route to confirm the
    # Web Service is actually running (and therefore not put to sleep).
    return 'VedaVPN bot is running ✅', 200


@app.route('/sub', methods=['GET'])
def get_sub():
    # Կարդալ sub ֆայլը և վերադարձնել որպես տեքստ
    file_path = os.path.join(os.path.dirname(__file__), 'sub')
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return content, 200, {'Content-Type': 'text/plain; charset=utf-8'}
    except FileNotFoundError:
        return "File not found", 404


def setup_webhook():
    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(url=WEBHOOK_URL, allowed_updates=["message", "callback_query", "chat_member"])
    print(f"✅ Webhook-ը սահմանվեց՝ {WEBHOOK_URL}")


# Both of these run at module load time (i.e. also when gunicorn
# imports `bot:app`, not only when running `python bot.py`
# directly).
init_db()
setup_webhook()


if __name__ == '__main__':
    # For local testing. On Render, gunicorn runs `app`,
    # this block is not called there.
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
