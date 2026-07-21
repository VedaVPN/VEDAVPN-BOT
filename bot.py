import csv
import hashlib
import logging
import os
import random
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from io import BytesIO
import socket
from urllib.parse import unquote, quote, urlparse

import psycopg2
import psycopg2.pool
import qrcode
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException
from flask import Flask, request, abort
from github import Github
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vedavpn")

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

# Գաղտնի token՝ webhook-ի իսկությունը ստուգելու համար։ Telegram-ը այն ուղարկում է
# X-Telegram-Bot-Api-Secret-Token header-ով։ Կարելի է սահմանել WEBHOOK_SECRET env-ով,
# հակառակ դեպքում ավտոմատ ստացվում է BOT_TOKEN-ից։
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET') or hashlib.sha256(f"vedavpn:{TOKEN or ''}".encode()).hexdigest()[:48]

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
        try:
            cur.execute(query, params)
            result = None
            if fetchone:
                result = cur.fetchone()
            elif fetchall:
                result = cur.fetchall()
            if commit:
                conn.commit()
            return result
        finally:
            cur.close()
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
    # Գրանցման ամսաթիվ՝ «նոր օգտատերեր այսօր/շաբաթ» վիճակագրության համար
    db_execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT now()", commit=True)
    # Անհատական sub հղումների սյուներ՝ token, ban, օգտագործման հաշվիչ
    db_execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS sub_token TEXT", commit=True)
    db_execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS banned BOOLEAN DEFAULT FALSE", commit=True)
    db_execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS sub_fetches BIGINT DEFAULT 0", commit=True)
    db_execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_sub_at TIMESTAMP", commit=True)
    db_execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_sub_token ON users (sub_token)", commit=True)
    # Վերջին ակտիվության պահը՝ ավտոմատ ողջույնի համար
    db_execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen TIMESTAMP", commit=True)
    # Օգտատիրոջ հերթական համարը («Դու №N օգտատերն ես»)
    db_execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS member_no BIGINT", commit=True)
    # Ծննդյան օր (DD.MM) և վերջին շնորհավորած տարին
    db_execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS birthday TEXT", commit=True)
    db_execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS bday_greeted_year INTEGER", commit=True)
    # Ռեֆերալների գրանցամատյան՝ «Ամսվա չեմպիոն»-ի համար
    db_execute('''CREATE TABLE IF NOT EXISTS referral_log
                (id BIGSERIAL PRIMARY KEY,
                 referrer_id BIGINT,
                 ts TIMESTAMP DEFAULT now())''', commit=True)
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
    # Captcha-ի և support-ի ժամանակավոր state-ը պահվում է DB-ում (ոչ in-memory),
    # որպեսզի gunicorn-ի մի քանի worker-ների դեպքում էլ ճիշտ աշխատի։
    db_execute('''CREATE TABLE IF NOT EXISTS pending_captcha
                (user_id BIGINT PRIMARY KEY,
                 answer INTEGER,
                 ref_id BIGINT DEFAULT 0,
                 ts DOUBLE PRECISION)''', commit=True)
    db_execute('''CREATE TABLE IF NOT EXISTS pending_support
                (user_id BIGINT PRIMARY KEY,
                 from_user_id BIGINT,
                 username TEXT,
                 chat_id BIGINT,
                 message_id BIGINT,
                 ts DOUBLE PRECISION)''', commit=True)
    # ⭐ Օգտատերերի գնահատականներն ու մեկնաբանությունները (1 գնահատական՝ 1 օգտատեր)
    db_execute('''CREATE TABLE IF NOT EXISTS feedback
                (user_id BIGINT PRIMARY KEY,
                 rating INTEGER,
                 comment TEXT,
                 updated_at TIMESTAMP DEFAULT now())''', commit=True)
    # Օգտատիրոջ անունը՝ հրապարակային կարծիքների ցուցակի համար
    db_execute("ALTER TABLE feedback ADD COLUMN IF NOT EXISTS name TEXT", commit=True)
    # Ադմինի պատասխանը կարծիքին (/freply)
    db_execute("ALTER TABLE feedback ADD COLUMN IF NOT EXISTS reply TEXT", commit=True)
    # Մեկնաբանության սպասման state-ը՝ DB-ում (gunicorn worker-ների համար անվտանգ)
    db_execute('''CREATE TABLE IF NOT EXISTS pending_feedback
                (user_id BIGINT PRIMARY KEY,
                 ts DOUBLE PRECISION)''', commit=True)
    # Broadcast-ի հերթը՝ DB-ում (ոչ միայն հիշողությունում), որպեսզի Render-ի
    # restart/sleep-ի դեպքում էլ չկորչի կիսատ մնացած broadcast-ը։
    db_execute('''CREATE TABLE IF NOT EXISTS broadcast_queue
                (user_id BIGINT PRIMARY KEY,
                 text TEXT,
                 photo_id TEXT,
                 video_id TEXT)''', commit=True)


# === CONTENT (bilingual texts, editable by the admin) ===
CONTENT_DEFAULTS = {
    'btn_get_vpn':   {'hy': "🛡 Ստանալ VPN", 'ru': "🛡 Получить VPN"},
    'btn_referrals': {'hy': "👥 Ռեֆերալներ", 'ru': "👥 Рефералы"},
    'btn_howto':     {'hy': "📖 Ինչպես տեղադրել", 'ru': "📖 Как установить"},
    'btn_faq':       {'hy': "❓ Հաճախ տրվող հարցեր", 'ru': "❓ Часто задаваемые вопросы"},
    'btn_support':   {'hy': "🆘 Աջակցություն", 'ru': "🆘 Поддержка"},
    'btn_forum':     {'hy': "💬 Ֆորում", 'ru': "💬 Чат (ФОРУМ)"},
    'btn_iptv':      {'hy': "📺 IPTV", 'ru': "📺 IPTV"},
    'btn_info':      {'hy': "📜 Պայմաններ և գաղտնիություն", 'ru': "📜 Условия и конфиденциальность"},
    'btn_terms':     {'hy': "📄 Օգտագործման պայմաններ", 'ru': "📄 Условия использования"},
    'btn_privacy':   {'hy': "🔒 Գաղտնիության քաղաքականություն", 'ru': "🔒 Политика конфиденциальности"},
    'btn_back':      {'hy': "⬅️ Հետ", 'ru': "⬅️ Назад"},
    'btn_main_menu': {'hy': "🏠 Գլխավոր մենյու", 'ru': "🏠 Главное меню"},
    'btn_adblock':   {'hy': "🛡 Առանց Գովազդի", 'ru': "🛡 Без рекламы"},

    'text_adblock': {
        'hy': (
            "🛡 <b>Առանց Գովազդի (AdBlock)</b>\n\n"
            "Վեբ-կայքերում և խաղերում գովազդներից ընդմիշտ ազատվելու համար, փոխեք DNS կարգավորումները Ձեր VPN հավելվածում.\n\n"
            "<b>📱 Happ կամ INCY հավելվածներում.</b>\n"
            "1. Բացեք հավելվածի կարգավորումները (Settings)\n"
            "2. Գտեք <b>Custom DNS</b> կամ <b>DNS</b> բաժինը\n"
            "3. Միացրեք այն և մուտքագրեք հետևյալ IP հասցեն՝\n"
            "<code>94.140.14.14</code>\n\n"
            "✅ Պահպանեք և վերամիացեք VPN-ին: Գովազդներն այլևս չեն խանգարի Ձեզ!"
        ),
        'ru': (
            "🛡 <b>Без рекламы (AdBlock)</b>\n\n"
            "Чтобы навсегда избавиться от рекламы на сайтах и в играх, измените настройки DNS в вашем VPN-приложении.\n\n"
            "<b>📱 В приложениях Happ или INCY:</b>\n"
            "1. Откройте настройки (Settings) приложения\n"
            "2. Найдите раздел <b>Custom DNS</b> или <b>DNS</b>\n"
            "3. Включите его и введите следующий IP-адрес:\n"
            "<code>94.140.14.14</code>\n\n"
            "✅ Сохраните и переподключите VPN. Реклама больше не будет вас беспокоить!"
        ),
    },

    'text_choose_lang': {
        'hy': "🌍 Ընտրեք լեզուն / Выберите язык:",
        'ru': "🌍 Ընտրեք լեզուն / Выберите язык:",
    },
    'text_subscribe_warn': {
        'hy': "⚠️ Խնդր��ւմ ենք բաժանորդագրվել ալիքին և սեղմել ստուգելու կոճակը:",
        'ru': "⚠️ Пожалуйста, подпишитесь на канал и нажмите кнопку провер��и:",
    },
    'text_not_subscribed': {
        'hy': "❌ Դուք դեռ բաժանորդագրված չեք:",
        'ru': "❌ Вы еще не подписались:",
    },
    'text_vpn_caption': {
        'hy': "🔗 <b>VPN հղում:</b>\n<code>{link}</code>\n\nℹ️ Չգիտե����ք որտեղ տեղադրել այս հղումը։ Սեղմեք «📖 Ինչպես տեղադրել» կոճակը menu-ից։",
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
    'text_info_prompt': {
        'hy': "📜 Ընտրեք՝",
        'ru': "📜 Выберите:",
    },
    'text_terms': {
        'hy': (
            "📄 <b>Օգտագործման պայմաններ</b>\n\n"
            "Այս Telegram բոտն ու դրանով տրամադրվող VedaVPN ծառայությունը օգտագործելով՝ դուք համաձայնվում եք հետևյալ պայմաններին։\n\n"
            "<b>1. Ծառայության նկարագրություն</b>\n"
            "VedaVPN-ը անվճար VPN և IPTV հասանելի��ւթյուն է տրամադրում մեր Telegram ալիքի բաժանորդներին՝ որպես bonus։ Ծառայությունը կարող է փոփոխվել, սահմանափակվել կամ ��ադարեցվել ցանկացած պահի, առանց նախնական ծանուցման։\n\n"
            "<b>2. Օգտվելու պայման</b>\n"
            "VPN հղումը ակտիվ մնալու համար անհրաժեշտ է մնալ բաժանորդագրված մեր ալիքին։ Ալիքից դուրս գալու դեպքում հասանելիությունը կարող է սահմանափակվել։\n\n"
            "<b>3. Թույլատրելի օգտագործում</b>\n"
            "Արգելվում է ծառայությունն օգտագործել ապօրինի գործունեության, երրորդ անձան������ իրավունքների խախտման կամ վնասակար նպատակներով։\n\n"
            "<b>4. Երաշխիքի բացակայություն</b>\n"
            "Ծառայությունը տրամադրվում է «ինչպես կա» սկզբունքով, առանց արագության, անընդհատության կամ որակի երաշխիքի։ Մենք պատասխանատվություն չենք կրում հնարավոր ընդհատումների, տվյալների կորստի կամ վնասների համար։\n\n"
            "<b>5. Փոփոխություններ</b>\n"
            "Այս պայմանները կարող են ժամանակ առ ժամանակ թարմացվել։ Ծառայությունից շարունակական օգտվելը նշանակում է թարմացված պայմանների ընդունում։\n\n"
            "<b>6. Կապ</b>\n"
            "Հարցերի դեպքում գրեք «🆘 Աջակցություն» կոճակով։"
        ),
        'ru': (
            "📄 <b>Условия использования</b>\n\n"
            "Используя этого Telegram-бота и сервис VedaVPN, вы соглашаетесь со следующими условиями.\n\n"
            "<b>1. Описание сервиса</b>\n"
            "VedaVPN предоставляет бесплатный доступ к VPN и IPTV подписчикам нашего Telegram-канала в качестве бонуса. Сервис может быть изменён, ограничен или прекращён в любой мо����ент без предварительного уведомления.\n\n"
            "<b>2. Усл������вие пользования</b>\n"
            "Для сохранения активной VPN-ссылки необходимо оставаться подписанным на наш канал. При отписке доступ может быть ограничен.\n\n"
            "<b>3. Допустимое использование</b>\n"
            "Запрещается использовать сервис для незаконной деятельности, нарушения прав третьих лиц или во вредоносных целях.\n\n"
            "<b>4. Отказ от гарантий</b>\n"
            "Сервис предоставляется «как есть», без гарантий скорости, бесперебойности или качества. Мы не несём ответственности за возможные перебои, потерю данных или ущерб.\n\n"
            "<b>5. Изменения</b>\n"
            "Эти условия могут периодически обновляться. Продолжение использования сервиса означает согласие с обновлёнными условиями.\n\n"
            "<b>6. Контакты</b>\n"
            "По всем вопросам пишите через кнопку «🆘 Поддержка»."
        ),
    },
    'text_privacy': {
        'hy': (
            "🔒 <b>Գաղտնիության քաղաքականություն</b>\n\n"
            "<b>1. Ի՞նչ տվյալներ ենք հավաքագրում</b>\n"
            "• Ձեր Telegram ID և, առկայության դեպքում, username\n"
            "• Ընտրած լեզուն և սարքի տեսակը (Android/iPhone)\n"
            "• Ռեֆերալ տվյալներ (ում եք հրավիրել/ում կողմից եք հրավիրվել)\n"
            "• Բոտին ուղարկած հաղորդագրությունները (օր. աջակցության հարցումներ)\n\n"
            "<b>2. Ինչի՞ համար ենք օգտագործում</b>\n"
            "Այս տվյալներն օգտագործվում են բացառապես ծառայությունը մատուցելու համար՝ ալիքի բաժանորդագրությունը ստուգելու, ճիշտ լեզվով ու հղումով պատասխանելու, ռեֆերալները հաշվելու և աջակցության հարցումներին պատասխանելու նպատակով։ Աջակցության բաժնում ուղարկած հաղորդագրությունները փոխանցվում են ադմինիստրատորին՝ ձեր ID-ի և պրոֆիլի հղումի հետ միասին։\n\n"
            "<b>3. Ի՞նչ ՉԵՆՔ հավաքագրում</b>\n"
            "Բոտն ինքնին չի հավաքագրում և չի պահպանում ձեր browsing/ինտերնետային ակտիվության տվյալները։ VPN սերվերի աշխատանքն ապահովվում է առանձին ենթակառուցվածքով։\n\n"
            "<b>4. Տվյալների պահպանում և փոխանցում</b>\n"
            "Տվյալները պահվում են անվտանգ տվյալների բազայում այնքան ժամանակ, քանի դեռ օգտվում եք ծառայությունից։ Մենք չենք վաճառում և չենք փոխանցում ձեր տվյալները երրորդ անձանց, բացառությամբ օրենքով պահանջվող դեպքերի։\n\n"
            "<b>5. Ձեր իրավունքները</b>\n"
            "Կարող եք ցանկացած պահի հարցնել՝ ինչ տվյալներ ենք պահում ձեր մասին, կամ պահանջել դրանց ջնջում՝ գրելով «🆘 Աջակցություն» կոճակով։\n\n"
            "<b>6. Փոփոխություններ</b>\n"
            "Այս քաղաքականությունը կարող է թարմացվել։ Փոփոխություններից հետո շարունակական օգտագործումը նշանակում է համաձայնություն։"
        ),
        'ru': (
            "🔒 <b>Политика конфиденциальности</b>\n\n"
            "<b>1. Какие данные мы собираем</b>\n"
            "• Ваш Telegram ID и, если есть, username\n"
            "• Выбранный язык и тип устройства (Android/iPhone)\n"
            "• Реферальные данные (кого вы пригласили / кем приглашены)\n"
            "• Сообщения, отправленные боту (например, запросы в поддержку)\n\n"
            "<b>2. Для чего мы их используем</b>\n"
            "Эти данные используются исключительн�� для работы сервиса: проверки подписки на канал, ответа на нужном языке со ссылкой, подсчёта рефералов и обработки обращений в поддержку. Сообщения, отправленные в поддержку, пересылаются администратору вместе с вашим ID и ссылкой на профиль.\n\n"
            "<b>3. Что мы НЕ собираем</b>\n"
            "Сам бот не собирает и не хранит данные о вашей интернет-активности/трафике. Работа VPN-сервера обеспечивается отдельной инфраструктурой.\n\n"
            "<b>4. Хранение и передача данных</b>\n"
            "Данные хранятся в защищённой базе данных, пока вы пользуетесь сервисом. Мы не продаём и не передаём ваши данные третьим лицам, за исключением случаев, предусмотренных законом.\n\n"
            "<b>5. Ваши права</b>\n"
            "Вы можете в любой момент узнать, какие данные о вас хранятся, или запросить их удаление, написав через кнопку «🆘 Поддержка».\n\n"
            "<b>6. Изменения</b>\n"
            "Эта политика может обновляться. Дальнейшее использование сервиса после изменений означает согласие с ними."
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
            "⚠️ <b>Вы отпис��лись от канала</b>\n\n"
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

# === ENGLISH CONTENT ===
# Անգլերեն տեքստերը առանձին dict-ում են, ապա merge են արվում CONTENT_DEFAULTS-ի մեջ։
CONTENT_EN = {
    'btn_get_vpn':   "🛡 Get VPN",
    'btn_referrals': "👥 Referrals",
    'btn_howto':     "📖 How to install",
    'btn_faq':       "❓ FAQ",
    'btn_support':   "🆘 Support",
    'btn_forum':     "💬 Forum",
    'btn_iptv':      "📺 IPTV",
    'btn_info':      "📜 Terms & Privacy",
    'btn_terms':     "📄 Terms of Use",
    'btn_privacy':   "🔒 Privacy Policy",
    'btn_back':      "⬅️ Back",
    'btn_adblock':   "🛡 AdBlock",
    'text_adblock': (
        "🛡 <b>AdBlock</b>\n\n"
        "To get rid of ads on websites and in games forever, change the DNS settings in your VPN app.\n\n"
        "<b>📱 In Happ or INCY apps:</b>\n"
        "1. Open the app settings (Settings)\n"
        "2. Find the <b>Custom DNS</b> or <b>DNS</b> section\n"
        "3. Enable it and enter the following IP address:\n"
        "<code>94.140.14.14</code>\n\n"
        "✅ Save and reconnect to the VPN. Ads will no longer bother you!"
    ),
    'btn_main_menu': "🏠 Main menu",
    'text_choose_lang': "🌍 Ընտրեք լեզուն / Выберите язык / Choose language:",
    'text_subscribe_warn': "⚠️ Please subscribe to the channel and press the check button:",
    'text_not_subscribed': "❌ You are not subscribed yet:",
    'text_vpn_caption': "🔗 <b>Your VPN link:</b>\n<code>{link}</code>\n\nℹ️ Not sure where to paste this link? Tap «📖 How to install» in the menu.",
    'text_howto': (
        "📖 <b>How to add the VPN link</b>\n\n"
        "1️⃣ First copy the VPN link by tapping it in the bot message\n\n"
        "<b>📱 In the Happ app:</b>\n"
        "• Open Happ\n"
        "• Tap the «+» button in the top right\n"
        "• Choose «Add from Clipboard» or «Import from URL»\n"
        "• Confirm (the link is already copied)\n"
        "• Pick a server from the list and tap Connect\n\n"
        "<b>📱 In the INCY app:</b>\n"
        "• Open INCY\n"
        "• Tap «+»\n"
        "• Choose «Paste from Clipboard» or «Add Subscription»\n"
        "• Confirm the link\n"
        "• Pick a server and connect"
    ),
    'text_faq': (
        "❓ <b>Frequently Asked Questions</b>\n\n"
        "<b>1. How do I get the VPN link?</b>\n"
        "Tap «🛡 Get VPN» and subscribe to the channel — the link is sent automatically.\n\n"
        "<b>2. Why do I need to subscribe to the channel?</b>\n"
        "The VPN is provided to channel subscribers for free as a bonus.\n\n"
        "<b>3. Is the VPN paid?</b>\n"
        "No — it is completely free.\n\n"
        "<b>4. How do I add the link to the app?</b>\n"
        "Tap «📖 How to install» in the menu for step-by-step instructions.\n\n"
        "<b>5. The VPN is not working, what should I do?</b>\n"
        "Try picking another server in the app. If that doesn't help, contact «🆘 Support».\n\n"
        "<b>6. How do I get more bonuses?</b>\n"
        "Invite friends with your referral link (see «👥 Referrals»)."
    ),
    'text_info_prompt': "📜 Choose:",
    'text_terms': (
        "📄 <b>Terms of Use</b>\n\n"
        "By using this Telegram bot and the VedaVPN service you agree to the following terms.\n\n"
        "<b>1. Service description</b>\n"
        "VedaVPN provides free VPN and IPTV access to subscribers of our Telegram channel as a bonus. The service may be changed, limited, or discontinued at any time without prior notice.\n\n"
        "<b>2. Usage requirement</b>\n"
        "To keep your VPN link active you must stay subscribed to our channel. If you unsubscribe, access may be restricted.\n\n"
        "<b>3. Acceptable use</b>\n"
        "Using the service for illegal activity, violating third-party rights, or any harmful purposes is prohibited.\n\n"
        "<b>4. No warranty</b>\n"
        "The service is provided «as is», with no guarantees of speed, availability, or quality. We are not liable for possible interruptions, data loss, or damages.\n\n"
        "<b>5. Changes</b>\n"
        "These terms may be updated from time to time. Continued use of the service means you accept the updated terms.\n\n"
        "<b>6. Contact</b>\n"
        "For any questions use the «🆘 Support» button."
    ),
    'text_privacy': (
        "🔒 <b>Privacy Policy</b>\n\n"
        "<b>1. What data we collect</b>\n"
        "• Your Telegram ID and username (if any)\n"
        "• Chosen language and device type (Android/iPhone)\n"
        "• Referral data (who you invited / who invited you)\n"
        "• Messages sent to the bot (e.g. support requests)\n\n"
        "<b>2. How we use it</b>\n"
        "This data is used solely to run the service: checking channel subscription, replying in the right language with your link, counting referrals, and handling support requests. Support messages are forwarded to the administrator together with your ID and profile link.\n\n"
        "<b>3. What we do NOT collect</b>\n"
        "The bot itself does not collect or store your browsing/traffic data. The VPN servers are run on separate infrastructure.\n\n"
        "<b>4. Storage and sharing</b>\n"
        "Data is stored in a secure database for as long as you use the service. We do not sell or share your data with third parties, except where required by law.\n\n"
        "<b>5. Your rights</b>\n"
        "You can ask at any time what data we store about you, or request its deletion, via the «🆘 Support» button.\n\n"
        "<b>6. Changes</b>\n"
        "This policy may be updated. Continued use of the service after changes means you agree to them."
    ),
    'text_support_prompt': "📩 Write your question:",
    'text_welcome': "👋 Welcome to VedaVPN! Free and secure VPN for our channel subscribers.",
    'text_iptv_caption': "📺 <b>IPTV link:</b>\n<code>{link}</code>",
    'text_iptv_missing': "❌ IPTV link is not set yet.",
    'text_iptv_instructions': (
        "\n\nℹ️ <b>How to connect:</b>\n"
        "1️⃣ Copy the link above\n"
        "2️⃣ Open your IPTV app (e.g. IPTV Smarters, TiviMate)\n"
        "3️⃣ Choose «Add Playlist�� / «Add from URL»\n"
        "4️⃣ Paste the link and confirm\n"
        "5️⃣ Wait for the channels to load and enjoy"
    ),
    'text_unsub_warning': (
        "⚠️ <b>You left the channel</b>\n\n"
        "The VPN is available only to subscribers of our channel. "
        "If you want to keep using the VPN, please subscribe again."
    ),
    'btn_device_android': "🤖 Android",
    'btn_device_ios':     "🍏 iPhone",
    'btn_open_happ':      "🚀 Open in Happ",
    'btn_open_incy':      "🚀 Open in INCY",
    'btn_store_google':   "⬇️ Google Play",
    'btn_store_apple':    "⬇️ App Store",
}

for _key, _value in CONTENT_EN.items():
    CONTENT_DEFAULTS.setdefault(_key, {})['en'] = _value


def get_content(key, lang):
    row = db_execute("SELECT value FROM content WHERE key = %s AND lang = %s", (key, lang), fetchone=True)
    if row:
        return row[0]
    entry = CONTENT_DEFAULTS.get(key, {})
    return entry.get(lang) or entry.get('ru') or f"[{key}/{lang}]"


def tr(lang, hy, ru, en=None):
    """Կարճ helper՝ երեք լեզվով inline տեքստերի համար (en-ի բացակայության դեպքում՝ ru)։"""
    if lang == 'hy':
        return hy
    if lang == 'en' and en is not None:
        return en
    return ru


def is_menu_btn(text, key):
    """Ստուգում է՝ տեքստը մենյուի կոճակ է որևէ լեզվով։"""
    return text in (get_content(key, 'hy'), get_content(key, 'ru'), get_content(key, 'en'))


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


def get_or_create_sub_token(user_id):
    """Վերադարձնում է օգտատիրոջ անհատական sub token-ը, անհրաժեշտության դեպքում ստեղծում է։"""
    row = db_execute("SELECT sub_token FROM users WHERE user_id = %s", (user_id,), fetchone=True)
    if row and row[0]:
        return row[0]
    if not row:
        return None
    token = secrets.token_urlsafe(12)
    db_execute(
        "UPDATE users SET sub_token = %s WHERE user_id = %s AND sub_token IS NULL",
        (token, user_id), commit=True
    )
    row = db_execute("SELECT sub_token FROM users WHERE user_id = %s", (user_id,), fetchone=True)
    return row[0] if row and row[0] else None


def get_personal_sub_link(user_id):
    """Անհատական sub հղում. եթե token ��կա, fallback՝ ընդհանուր հղումը։"""
    token = get_or_create_sub_token(user_id)
    if token:
        return f"{WEBHOOK_HOST}/sub/{token}"
    return get_config('vpn_link')


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
    markup.add(get_content('btn_iptv', lang), get_content('btn_info', lang))

    custom = db_execute("SELECT label_hy, label_ru FROM custom_buttons", fetchall=True) or []
    for label_hy, label_ru in custom:
        markup.add(label_hy if lang == 'hy' else label_ru)

    return markup


# === «Քարտային» դիզայն և inline գլխավոր մենյու ===
SEP = "▬▬▬▬▬▬▬▬▬▬▬▬"


def card(title, body):
    """Միասնական «քարտային» ոճ բոլոր բաժինների համար։"""
    return f"🛡 <b>VedaVPN</b> │ <b>{title}</b>\n{SEP}\n\n{body}"


def edit_or_send(chat_id, message_id, text, markup):
    """In-place նավիգացիա. խմբագրում է նույն հաղորդագրությունը, fallback՝ նոր հաղորդագրություն։"""
    if message_id:
        try:
            bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                  reply_markup=markup, disable_web_page_preview=True)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)


def build_main_menu_inline(lang):
    """Գլխավոր մենյուն որպես inline ստեղնաշար՝ 2 սյունակով։"""
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton(get_content('btn_get_vpn', lang), callback_data="menu_vpn"),
        types.InlineKeyboardButton(get_content('btn_referrals', lang), callback_data="menu_refs"),
    )
    markup.add(
        types.InlineKeyboardButton(get_content('btn_howto', lang), callback_data="menu_howto"),
        types.InlineKeyboardButton(get_content('btn_faq', lang), callback_data="menu_faq"),
    )
    markup.add(
        types.InlineKeyboardButton(get_content('btn_support', lang), callback_data="menu_support"),
        types.InlineKeyboardButton(get_content('btn_forum', lang), callback_data="menu_forum"),
    )
    markup.add(
        types.InlineKeyboardButton(get_content('btn_iptv', lang), callback_data="menu_iptv"),
        types.InlineKeyboardButton(get_content('btn_info', lang), callback_data="menu_info"),
    )
    markup.add(
        types.InlineKeyboardButton(get_content('btn_adblock', lang), callback_data="menu_adblock"),
    )
    custom = db_execute("SELECT id, label_hy, label_ru FROM custom_buttons", fetchall=True) or []
    for btn_id, label_hy, label_ru in custom:
        markup.add(types.InlineKeyboardButton(label_hy if lang == 'hy' else label_ru,
                                              callback_data=f"menu_cbtn_{btn_id}"))
    markup.add(types.InlineKeyboardButton(
        tr(lang, "⭐ Գնահատիր բոտը", "⭐ Оцени бота", "⭐ Rate the bot"),
        callback_data="menu_rate"))
    return markup


def trust_line(lang):
    """🤝 «Մեզ վստահում է X մարդ» + միջին գնահատական՝ գլխավոր մենյուի քարտի համար։"""
    try:
        row = db_execute("SELECT COUNT(*) FROM users", fetchone=True)
        total = row[0] if row else 0
    except Exception:
        total = 0
    if not total:
        return ""
    line = tr(lang,
              f"🤝 Մեզ վստահում է <b>{total}</b> մարդ",
              f"🤝 Нам доверяют <b>{total}</b> человек",
              f"🤝 Trusted by <b>{total}</b> people")
    try:
        stats = db_execute("SELECT COUNT(*), AVG(rating) FROM feedback WHERE rating IS NOT NULL", fetchone=True)
        if stats and stats[0]:
            avg = float(stats[1] or 0)
            line += f" • 🌟 {avg:.1f}/5"
    except Exception:
        pass
    return line + "\n\n"


def get_member_no(user_id):
    """Օգտատիրոջ հերթական համարը՝ «Դու №N օգտատերն ես»։ Հաշվվում է մեկ անգամ և պահպանվում։"""
    row = db_execute("SELECT member_no FROM users WHERE user_id = %s", (user_id,), fetchone=True)
    if not row:
        return None
    if row[0]:
        return row[0]
    rank_row = db_execute(
        "SELECT COUNT(*) FROM users WHERE (created_at, user_id) <= "
        "(SELECT created_at, user_id FROM users WHERE user_id = %s)",
        (user_id,), fetchone=True)
    no = rank_row[0] if rank_row else None
    if no:
        db_execute("UPDATE users SET member_no = %s WHERE user_id = %s", (no, user_id), commit=True)
    return no


def send_main_menu(chat_id, lang, message_id=None):
    """Գլխավոր մենյու՝ սիրուն «քարտով». հնարավորության դեպքում խմբագրում է նույն հաղորդագրությունը։"""
    hello = tr(lang, "Ընտրիր բաժինը 👇", "Выбери раздел 👇", "Choose a section 👇")
    no = get_member_no(chat_id)
    no_line = ""
    if no:
        no_line = tr(lang,
                     f"🔢 Դու №{no} օգտատերն ես",
                     f"🔢 Ты пользователь №{no}",
                     f"🔢 You are user #{no}") + "\n\n"
    text = f"🛡 <b>VedaVPN</b>\n{SEP}\n\n{trust_line(lang)}{no_line}{hello}"
    edit_or_send(chat_id, message_id, text, build_main_menu_inline(lang))


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


def show_typing(chat_id):
    """«Տպում է...» ինդիկատոր՝ ավելի կենդանի զգացողության համար։"""
    try:
        bot.send_chat_action(chat_id, 'typing')
    except Exception:
        pass


def hide_main_menu(chat_id):
    """Փակում է ներքևի գլխավոր մենյուն (ReplyKeyboard), որ inline «🏠 Գլխավոր մենյու»-ն իմաստ ունենա։"""
    try:
        msg = bot.send_message(chat_id, "⌨️", reply_markup=types.ReplyKeyboardRemove())
    except Exception:
        return
    try:
        bot.delete_message(chat_id, msg.message_id)
    except Exception:
        pass


def build_nav_markup(lang, back_callback=None, extra_rows=None):
    """Միասնական navigation՝ ընտրովի «⬅️ Հետ» + միշտ «🏠 Գլխավոր մենյու»։"""
    markup = types.InlineKeyboardMarkup()
    if extra_rows:
        for btn in extra_rows:
            markup.add(btn)
    nav = []
    if back_callback:
        nav.append(types.InlineKeyboardButton(get_content("btn_back", lang), callback_data=back_callback))
    nav.append(types.InlineKeyboardButton(get_content("btn_main_menu", lang), callback_data="main_menu"))
    markup.add(*nav)
    return markup


def build_info_markup(lang):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton(get_content("btn_terms", lang), callback_data="info_terms"),
        types.InlineKeyboardButton(get_content("btn_privacy", lang), callback_data="info_privacy"),
    )
    markup.add(types.InlineKeyboardButton(get_content("btn_main_menu", lang), callback_data="main_menu"))
    return markup


def build_back_markup(lang, callback_data="info_back"):
    """«⬅️ Հետ» + «🏠 Գլխավոր մենյու» կոճակներ՝ inline ենթաէջերի համար։"""
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(get_content("btn_back", lang), callback_data=callback_data),
        types.InlineKeyboardButton(get_content("btn_main_menu", lang), callback_data="main_menu"),
    )
    return markup

# === Ավտոմատ ողջույն՝ օրվա ժամին համապատասխան ===
TZ_OFFSET_HOURS = int(os.environ.get('TZ_OFFSET_HOURS', '3'))  # default՝ մոսկովյան ժամանակ (UTC+3)
GREET_GAP_HOURS = 6  # նվազագույն դադար (ժամ), որից հետո բոտը նորից կողջունի


def time_greeting(lang, name):
    """Օրվա ժամին համապատասխան ողջույն՝ 3 լեզվով։"""
    hour = (datetime.utcnow() + timedelta(hours=TZ_OFFSET_HOURS)).hour
    if 5 <= hour < 12:
        greet = tr(lang, "🌅 Բարի առավոտ", "🌅 Доброе утро", "🌅 Good morning")
    elif 12 <= hour < 18:
        greet = tr(lang, "☀️ Բարի օր", "☀️ Добрый день", "☀️ Good afternoon")
    elif 18 <= hour < 23:
        greet = tr(lang, "🌆 Բարի երեկո", "🌆 Добрый вечер", "🌆 Good evening")
    else:
        greet = tr(lang, "🌙 Բարի գիշեր", "🌙 Доброй ночи", "🌙 Good night")
    return f"{greet}, {name} 👋" if name else f"{greet} 👋"


def maybe_birthday(user_id, lang, first_name=""):
    """🎂 Տարին մեկ անգամ շնորհավորում է օգտատիրոջ ծննդյան օրը (մոսկովյան ժամով)։"""
    row = db_execute("SELECT birthday, bday_greeted_year FROM users WHERE user_id = %s", (user_id,), fetchone=True)
    if not row or not row[0]:
        return
    local_now = datetime.utcnow() + timedelta(hours=TZ_OFFSET_HOURS)
    if row[0] != local_now.strftime('%d.%m'):
        return
    if row[1] == local_now.year:
        return
    db_execute("UPDATE users SET bday_greeted_year = %s WHERE user_id = %s", (local_now.year, user_id), commit=True)
    name = (first_name or "").strip()
    nm = f", {name}" if name else ""
    msg = tr(lang,
             f"🎂🎉 Ծնունդդ շնորհավոր{nm}! Թող ինտերնետդ միշտ արագ լինի, իսկ կապը՝ անխափան 🥳 VedaVPN-ի թիմից 💙",
             f"🎂🎉 С днём рождения{nm}! Пусть интернет всегда будет быстрым, а соединение — стабильным 🥳 Команда VedaVPN 💙",
             f"🎂🎉 Happy birthday{nm}! May your internet always be fast and your connection rock-solid 🥳 From the VedaVPN team 💙")
    try:
        bot.send_message(user_id, msg)
    except Exception:
        pass


def daily_birthday_check():
    """🎂 Scheduler-ով, ամեն օր ֆիքսված ժամին, ստուգում է ԲՈԼՈՐ օգտատերերին մեկ անգամից՝
    այնպես որ նույնիսկ ով այդ օրը բոտ չի մտնում, դեռ կստանա շնորհավորանքը (ի տարբերություն
    maybe_birthday-ի, որը գործարկվում է միայն օգտատիրոջ ակտիվության պահին)։"""
    local_now = datetime.utcnow() + timedelta(hours=TZ_OFFSET_HOURS)
    today_str = local_now.strftime('%d.%m')
    curr_year = local_now.year
    rows = db_execute(
        "SELECT user_id, lang FROM users WHERE birthday = %s AND (bday_greeted_year IS NULL OR bday_greeted_year < %s)",
        (today_str, curr_year), fetchall=True) or []
    for uid, lang in rows:
        try:
            db_execute("UPDATE users SET bday_greeted_year = %s WHERE user_id = %s", (curr_year, uid), commit=True)
            msg = tr(lang,
                     "🎂🎉 Ծնունդդ շնորհավոր! Թող ինտերնետդ միշտ արագ լինի, իսկ կապը՝ անխափան 🥳 VedaVPN-ի թիմից 💙",
                     "🎂🎉 С днём рождения! Пусть интернет всегда будет быстрым, а соединение — стабильным 🥳 Команда VedaVPN 💙",
                     "🎂🎉 Happy birthday! May your internet always be fast and your connection rock-solid 🥳 From the VedaVPN team 💙")
            bot.send_message(uid, msg)
        except Exception:
            log.exception(f"Failed to send birthday wish to {uid}")


_CHAMP_CHECK_TS = {'ts': 0.0}


def check_month_champion():
    """🏅 Ամսվա սկզբին ավտոմատ որոշում է նախորդ ամսվա թոպ հրավիրողին ու շնորհավորում։"""
    now = datetime.utcnow()
    prev_last_day = now.replace(day=1) - timedelta(days=1)
    prev_month = prev_last_day.strftime('%Y-%m')
    if get_config('champion_month') == prev_month:
        return
    set_config('champion_month', prev_month)  # նախ նշում ենք, որ կրկնակի հայտարարություն չլինի
    row = db_execute(
        "SELECT referrer_id, COUNT(*) FROM referral_log "
        "WHERE to_char(ts, 'YYYY-MM') = %s "
        "GROUP BY referrer_id ORDER BY COUNT(*) DESC, referrer_id ASC LIMIT 1",
        (prev_month,), fetchone=True)
    if not row or not row[1]:
        set_config('champion_id', '')
        set_config('champion_count', '0')
        set_config('champion_name', '')
        return
    champ_id, champ_count = row[0], row[1]
    set_config('champion_id', str(champ_id))
    set_config('champion_count', str(champ_count))
    champ_name = ''
    try:
        champ_name = (bot.get_chat(champ_id).first_name or '').strip()[:64]
    except Exception:
        pass
    set_config('champion_name', champ_name)
    champ_lang = get_lang(champ_id)
    msg = tr(champ_lang,
             f"👑 Շնորհավո՜ր. դու անցած ամսվա ՉԵՄՊԻՈՆՆ ես՝ {champ_count} հրավերով 🏅 Շարունակիր նույն ոգով 💪",
             f"👑 Поздравляем! Ты ЧЕМПИОН прошлого месяца — {champ_count} приглашений 🏅 Так держать 💪",
             f"👑 Congrats! You are last month's CHAMPION with {champ_count} invites 🏅 Keep it up 💪")
    try:
        bot.send_message(champ_id, msg)
    except Exception:
        pass
    try:
        bot.send_message(ADMIN_ID,
                         f"🏅 Ամսվա չեմպիոն ({prev_month})՝ <code>{champ_id}</code> {fb_escape(champ_name)} — {champ_count} հրավեր",
                         parse_mode="HTML")
    except Exception:
        pass


def maybe_greet(user_id, first_name):
    """Ողջունում է օգտատիրոջն իր անունով, երբ նա բոտ է «մտնում» երկար դադարից հետո։"""
    row = db_execute("SELECT lang, last_seen FROM users WHERE user_id = %s", (user_id,), fetchone=True)
    if not row or not row[0]:
        return  # նոր օգտատեր է՝ /start-ի ողջույնը կաշխատի
    lang, last_seen = row[0], row[1]
    now = datetime.utcnow()
    db_execute("UPDATE users SET last_seen = %s WHERE user_id = %s", (now, user_id), commit=True)
    # 🎂 Ծննդյան օրվա ստուգում՝ ցանկացած ակտիվության պահին
    try:
        maybe_birthday(user_id, lang, first_name)
    except Exception:
        log.exception("birthday check failed")
    if last_seen is not None and (now - last_seen) < timedelta(hours=GREET_GAP_HOURS):
        return
    try:
        bot.send_message(user_id, time_greeting(lang, first_name))
    except Exception:
        log.exception("greeting failed")


def send_vpn_link(chat_id, lang):
    # Ամեն օգտատեր ստանում է ԻՐ անհատական հղումը (նույն բովանդակությամբ),
    # ինչը թույլ է տալիս անհրաժեշտության դեպքում անջատել կոնկրետ օգտատիրոջ (/ban)։
    link = get_personal_sub_link(chat_id)
    caption = get_content('text_vpn_caption', lang).format(link=link)
    qr_bio = generate_qr(link)
    show_typing(chat_id)
    hide_main_menu(chat_id)
    bot.send_photo(
        chat_id, qr_bio, caption=caption,
        reply_markup=build_app_markup(chat_id, lang),
    )
    done = tr(lang, "✅ Պատրաստ է՝ քո VPN հղումը վերևում է։", "✅ Готово — ваша VPN-ссылка выше.", "✅ Done — your VPN link is above.")
    bot.send_message(chat_id, done, reply_markup=build_nav_markup(lang))


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
    ok = tr(lang, "✅ Սարքը փոխվեց", "✅ Устройство изменено", "✅ Device changed")
    bot.answer_callback_query(call.id, ok)


# === CAPTCHA (only shown to brand-new users, to block bots/fake ref accounts) ===
# State-ը պահվում է DB-ում (ոչ թե in-memory dict-ում), որպեսզի gunicorn-ի
# մի քանի worker-ների դեպքում captcha-ն չկոտրվի։
CAPTCHA_TIMEOUT = 90  # seconds


def set_pending_captcha(user_id, answer, ref_id):
    db_execute(
        "INSERT INTO pending_captcha (user_id, answer, ref_id, ts) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (user_id) DO UPDATE SET answer = EXCLUDED.answer, ref_id = EXCLUDED.ref_id, ts = EXCLUDED.ts",
        (user_id, answer, ref_id, time.time()), commit=True
    )


def get_pending_captcha(user_id):
    row = db_execute("SELECT answer, ref_id, ts FROM pending_captcha WHERE user_id = %s", (user_id,), fetchone=True)
    if not row:
        return None
    return {'answer': row[0], 'ref_id': row[1], 'ts': row[2]}


def clear_pending_captcha(user_id):
    db_execute("DELETE FROM pending_captcha WHERE user_id = %s", (user_id,), commit=True)


def send_captcha(user_id, ref_id):
    a, b = random.randint(1, 9), random.randint(1, 9)
    correct = a + b

    wrong_pool = [correct + d for d in (-3, -2, -1, 1, 2, 3) if correct + d >= 0]
    wrong = random.sample(wrong_pool, 3)
    options = wrong + [correct]
    random.shuffle(options)

    set_pending_captcha(user_id, correct, ref_id)

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(*[types.InlineKeyboardButton(str(opt), callback_data=f"captcha_{opt}") for opt in options])
    bot.send_message(
        user_id,
        f"🤖 Հաստատեք, որ ռոբոտ չեք / Подтвердите, что вы не робот / Prove you are not a robot\n\n<b>{a} + {b} = ?</b>",
        reply_markup=markup
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith('captcha_'))
def process_captcha(call):
    user_id = call.from_user.id
    pending = get_pending_captcha(user_id)

    if not pending or time.time() - pending['ts'] > CAPTCHA_TIMEOUT:
        clear_pending_captcha(user_id)
        bot.answer_callback_query(call.id, "⏰ Ժամկետը լրացել է / Время истекло. /start")
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        return

    chosen = int(call.data.split('_')[1])
    if chosen != pending['answer']:
        bot.answer_callback_query(call.id, "❌ Սխալ / Неверно")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        send_captcha(user_id, pending['ref_id'])
        return

    ref_id = pending['ref_id']
    clear_pending_captcha(user_id)
    bot.answer_callback_query(call.id, "✅ OK")
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    finish_start(user_id, ref_id, call.from_user.first_name)


# === START ===
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.chat.id
    args = message.text.split()
    ref_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0

    # Only brand-new users need to pass the captcha; returning users skip straight through.
    already_registered = db_execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,), fetchone=True)
    if not already_registered:
        send_captcha(user_id, ref_id)
        return

    finish_start(user_id, ref_id, message.from_user.first_name)


def finish_start(user_id, ref_id, name=""):
    # RETURNING-ը ցույց է տալիս՝ օգտատերը իսկապես ՆՈՐ գրանցվե՞ց։ Ռեֆերալի
    # հաշվիչն ավելանում է ՄԻԱՅՆ նոր գրանցման դեպքում, որպեսզի հին օգտատերը
    # չկարողանա կրկնակի /start-երով արհեստականորեն ուռճացնել հաշվիչը։
    inserted = db_execute(
        "INSERT INTO users (user_id, ref_by) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING RETURNING user_id",
        (user_id, ref_id), fetchone=True, commit=True
    )
    if inserted is not None and ref_id > 0 and ref_id != user_id:
        db_execute("UPDATE users SET ref_count = ref_count + 1 WHERE user_id = %s", (ref_id,), commit=True)
        # Գրանցում ենք ամսաթվով՝ «Ամսվա չեմպիոն»-ի հաշվարկի համար
        db_execute("INSERT INTO referral_log (referrer_id) VALUES (%s)", (ref_id,), commit=True)
        notify_ref_progress(ref_id)

    # Remove the old menu buttons shown below, until the language is chosen
    welcome_photo = get_config('welcome_photo')
    welcome_caption = get_content('text_welcome', 'ru')
    name = (name or "").strip()
    if name:
        welcome_caption = f"👋 {name}!\n\n" + welcome_caption
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
    markup.add(types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"))
    bot.send_message(user_id, "👇", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith('lang_'))
def set_lang(call):
    lang = call.data.split('_')[1]
    db_execute("UPDATE users SET lang = %s WHERE user_id = %s", (lang, call.from_user.id), commit=True)
    name = (call.from_user.first_name or "").strip()
    if lang == 'hy':
        greeting = f"Բարև, {name} 👋 Բարի գալուստ VedaVPN 🛡" if name else "Բարև 👋 Բարի գալուստ VedaVPN 🛡"
    elif lang == 'en':
        greeting = f"Hi, {name} 👋 Welcome to VedaVPN 🛡" if name else "Hi 👋 Welcome to VedaVPN 🛡"
    else:
        greeting = f"Привет, {name} 👋 Добро пожаловать в VedaVPN 🛡" if name else "Привет 👋 Добро пожаловать в VedaVPN 🛡"
    bot.send_message(call.from_user.id, greeting, reply_markup=types.ReplyKeyboardRemove())
    send_main_menu(call.from_user.id, lang)


# === VPN CHECK ===
def sec_vpn(chat_id, lang, message_id=None):
    if not check_sub(chat_id):
        markup = types.InlineKeyboardMarkup()
        sub_label = tr(lang, "📢 Բաժանորդագրվել", "📢 Подписаться", "📢 Subscribe")
        check_label = tr(lang, "🔄 Ստուգել", "🔄 Проверить", "🔄 Check")
        markup.add(types.InlineKeyboardButton(sub_label, url="https://t.me/" + CHANNEL.replace("@", "")))
        markup.add(types.InlineKeyboardButton(check_label, callback_data="check_sub"))
        markup.add(types.InlineKeyboardButton(get_content("btn_main_menu", lang), callback_data="main_menu"))
        edit_or_send(chat_id, message_id, card(get_content('btn_get_vpn', lang), get_content('text_subscribe_warn', lang)), markup)
        return
    send_vpn_link(chat_id, lang)


@bot.message_handler(func=lambda m: is_menu_btn(m.text, 'btn_get_vpn'))
def get_vpn(message):
    lang = get_lang(message.chat.id)
    sec_vpn(message.chat.id, lang)


@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def check_sub_callback(call):
    lang = get_lang(call.from_user.id)
    if check_sub(call.from_user.id):
        bot.answer_callback_query(call.id, "✅ OK!")
        send_vpn_link(call.from_user.id, lang)
    else:
        bot.answer_callback_query(call.id, get_content('text_not_subscribed', lang))


# === HOW TO INSTALL ===
def sec_howto(chat_id, lang, message_id=None):
    edit_or_send(chat_id, message_id, card(get_content('btn_howto', lang), get_content('text_howto', lang)), build_nav_markup(lang))


@bot.message_handler(func=lambda m: is_menu_btn(m.text, 'btn_howto'))
def howto(message):
    lang = get_lang(message.chat.id)
    show_typing(message.chat.id)
    hide_main_menu(message.chat.id)
    sec_howto(message.chat.id, lang)


# === FAQ ===
def sec_faq(chat_id, lang, message_id=None):
    edit_or_send(chat_id, message_id, card(get_content('btn_faq', lang), get_content('text_faq', lang)), build_nav_markup(lang))


@bot.message_handler(func=lambda m: is_menu_btn(m.text, 'btn_faq'))
def faq(message):
    lang = get_lang(message.chat.id)
    show_typing(message.chat.id)
    hide_main_menu(message.chat.id)
    sec_faq(message.chat.id, lang)


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
            btn_text = tr(lang, "📢 Բաժանորդագրվել կրկին", "📢 Подписаться снова", "📢 Subscribe again")
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton(btn_text, url=f"https://t.me/{channel_username}"))
            bot.send_message(user_id, get_content('text_unsub_warning', lang), reply_markup=markup, parse_mode="HTML")
    except Exception:
        log.exception("membership change handling failed")


# === REFERRALS, FORUM ===
# === REFERRAL TIERS ===
# (շեմ, badge, հայերեն անուն, ռուսերեն անուն)
REF_TIERS = [
    (0, "🔰", "Սկսնակ", "Новичок", "Rookie"),
    (1, "🥉", "Բրոնզ", "Бронза", "Bronze"),
    (5, "🥈", "Արծաթ", "Серебро", "Silver"),
    (10, "🥇", "Ոսկի", "Золото", "Gold"),
    (20, "💎", "Ադամանդ", "Алмаз", "Diamond"),
]


def tier_name(tier, lang):
    if lang == 'hy':
        return tier[2]
    if lang == 'en':
        return tier[4]
    return tier[3]


def get_ref_tier(count, lang):
    """Վերադարձնում է ռեֆերալ մակարդակի badge-ը և progress-ը մինչև հաջորդ մակարդակը։"""
    current = REF_TIERS[0]
    nxt = None
    for i, tier in enumerate(REF_TIERS):
        if count >= tier[0]:
            current = tier
            nxt = REF_TIERS[i + 1] if i + 1 < len(REF_TIERS) else None
    badge = current[1]
    name = tier_name(current, lang)

    if nxt:
        need = nxt[0] - count
        span = nxt[0] - current[0]
        filled = int(((count - current[0]) / span) * 5) if span else 0
        filled = max(0, min(5, filled))
        bar = "▰" * filled + "▱" * (5 - filled)
        nxt_name = tier_name(nxt, lang)
        if lang == 'hy':
            progress = f"{bar}\n⬆️ Եւս {need} հրավեր → {nxt[1]} {nxt_name}"
        elif lang == 'en':
            progress = f"{bar}\n⬆️ {need} more invites → {nxt[1]} {nxt_name}"
        else:
            progress = f"{bar}\n⬆️ Ещё {need} приглашений → {nxt[1]} {nxt_name}"
    else:
        bar = "▰▰▰▰▰"
        top_msg = tr(lang, "👑 Առավելագույն մակարդակն է՝ հասած ես!", "👑 Достигнут максимальный уровень!", "👑 You've reached the top level!")
        progress = f"{bar}\n{top_msg}"

    if lang == 'hy':
        header = f"{badge} Ձեր մակարդակը՝ <b>{name}</b>"
    elif lang == 'en':
        header = f"{badge} Your level: <b>{name}</b>"
    else:
        header = f"{badge} Ваш уровень: <b>{name}</b>"
    return f"{header}\n{progress}"


def notify_ref_progress(ref_id):
    """Ծանուցում հրավիրողին նոր ռեֆերալի մասին. մակարդակի փոփոխության դեպքում՝ շնորհավորանք։"""
    try:
        row = db_execute("SELECT ref_count, lang FROM users WHERE user_id = %s", (ref_id,), fetchone=True)
        if not row:
            return
        count = row[0] or 0
        lang = row[1] or 'ru'
        tier = next((t for t in REF_TIERS if t[0] == count and t[0] > 0), None)
        if tier:
            t_name = tier_name(tier, lang)
            if lang == 'hy':
                msg = f"🎉 Շնորհավո՛ր, նոր մակարդակ՝ {tier[1]} <b>{t_name}</b> ({count} հրավեր)"
            elif lang == 'en':
                msg = f"🎉 Congrats, new level: {tier[1]} <b>{t_name}</b> ({count} invites)"
            else:
                msg = f"🎉 Поздравляем, новый уровень: {tier[1]} <b>{t_name}</b> ({count} приглаш.)"
        else:
            if lang == 'hy':
                msg = f"👥 +1 նոր ռեֆերալ։ Ընդամենը՝ {count}"
            elif lang == 'en':
                msg = f"👥 +1 new referral. Total: {count}"
            else:
                msg = f"👥 +1 новый реферал. Всего: {count}"
        bot.send_message(ref_id, msg)
    except Exception:
        log.exception("notify_ref_progress failed")


def sec_refs(chat_id, lang, message_id=None):
    row = db_execute("SELECT ref_count FROM users WHERE user_id = %s", (chat_id,), fetchone=True)
    count = row[0] if row else 0
    tier_line = get_ref_tier(count, lang)
    ref_link = "https://t.me/vedavpn_bot?start=" + str(chat_id)
    if lang == 'hy':
        text = f"👥 Ձեր հրավիրած օգտատերերը՝ {count}\n🔗 Հղում՝ {ref_link}"
        share_text = "🛡 Անվճար VPN VedaVPN-ից՝ միացիր իմ հղումով 👇"
        share_label = "📤 Կիսվել ընկերոջ հետ"
        lb_label = "🏆 Թոփ-10 հրավիրողներ"
    elif lang == 'en':
        text = f"👥 Your referrals: {count}\n🔗 Link: {ref_link}"
        share_text = "🛡 Free VPN from VedaVPN — join with my link 👇"
        share_label = "📤 Share with a friend"
        lb_label = "🏆 Top 10 inviters"
    else:
        text = f"👥 Ваших рефералов: {count}\n🔗 Ссылка: {ref_link}"
        share_text = "🛡 Бесплатный VPN от VedaVPN — подключайся по моей ссылке 👇"
        share_label = "📤 Поделиться с другом"
        lb_label = "🏆 Топ-10 приглашающих"
    share_url = "https://t.me/share/url?url=" + quote(ref_link) + "&text=" + quote(share_text)
    share_btn = types.InlineKeyboardButton(share_label, url=share_url)
    lb_btn = types.InlineKeyboardButton(lb_label, callback_data="ref_leaderboard")
    champ_line = ""
    champ_id = get_config('champion_id')
    if champ_id:
        champ_name = fb_escape(get_config('champion_name')) or tr(lang, "Օգտատեր", "Пользователь", "User")
        champ_count = get_config('champion_count') or "0"
        champ_line = tr(lang,
                        f"👑 Անցած ամսվա չեմպիոնը՝ <b>{champ_name}</b> ({champ_count} հրավեր)",
                        f"👑 Чемпион прошлого месяца: <b>{champ_name}</b> ({champ_count} пригл.)",
                        f"👑 Last month's champion: <b>{champ_name}</b> ({champ_count} invites)") + "\n\n"
    text = tier_line + "\n\n" + champ_line + text
    edit_or_send(chat_id, message_id, card(get_content('btn_referrals', lang), text), build_nav_markup(lang, extra_rows=[share_btn, lb_btn]))


@bot.message_handler(func=lambda m: is_menu_btn(m.text, 'btn_referrals'))
def show_refs(message):
    lang = get_lang(message.chat.id)
    show_typing(message.chat.id)
    hide_main_menu(message.chat.id)
    sec_refs(message.chat.id, lang)


@bot.callback_query_handler(func=lambda call: call.data == "ref_leaderboard")
def ref_leaderboard(call):
    """🏆 Թոփ-10 հրավիրողների leaderboardը։"""
    lang = get_lang(call.from_user.id)
    bot.answer_callback_query(call.id)
    top = db_execute(
        "SELECT user_id, ref_count FROM users WHERE ref_count > 0 ORDER BY ref_count DESC, user_id ASC LIMIT 10",
        fetchall=True
    ) or []
    medals = {1: "��", 2: "🥈", 3: "🥉"}
    if lang == 'hy':
        title = "🏆 <b>Թոփ-10 հրավիրողներ</b>"
        empty = "Դեռ ոչ ոք չունի հրավերներ։ Եղիր առաջինը 🚀"
        you_label = "Դու"
        unit = "հրավեր"
    elif lang == 'en':
        title = "🏆 <b>Top 10 inviters</b>"
        empty = "No one has invites yet. Be the first 🚀"
        you_label = "You"
        unit = "invites"
    else:
        title = "🏆 <b>Топ-10 приглашающих</b>"
        empty = "Пока ни у кого нет приглашений. Стань первым 🚀"
        you_label = "Вы"
        unit = "приглаш."
    if not top:
        edit_or_send(call.message.chat.id, call.message.message_id, f"{title}\n\n{empty}", build_nav_markup(lang, back_callback="menu_refs"))
        return
    lines = [title, ""]
    for i, (uid, cnt) in enumerate(top, 1):
        rank = medals.get(i, f"{i}.")
        tag = f" 👈 {you_label}" if uid == call.from_user.id else ""
        masked = f"…{str(uid)[-4:]}"
        lines.append(f"{rank} <code>{masked}</code> — {cnt} {unit}{tag}")
    if not any(uid == call.from_user.id for uid, _ in top):
        row = db_execute("SELECT ref_count FROM users WHERE user_id = %s", (call.from_user.id,), fetchone=True)
        my_count = row[0] if row and row[0] else 0
        if my_count > 0:
            rank_row = db_execute("SELECT COUNT(*) + 1 FROM users WHERE ref_count > %s", (my_count,), fetchone=True)
            my_rank = rank_row[0] if rank_row else "?"
            lines.append("")
            if lang == 'hy':
                lines.append(f"➖➖➖\n{you_label}՝ #{my_rank} տեղում ({my_count} {unit})")
            elif lang == 'en':
                lines.append(f"➖➖➖\n{you_label}: #{my_rank} place ({my_count} {unit})")
            else:
                lines.append(f"➖➖➖\n{you_label}: #{my_rank} место ({my_count} {unit})")
    edit_or_send(call.message.chat.id, call.message.message_id, "\n".join(lines), build_nav_markup(lang, back_callback="menu_refs"))


def sec_forum(chat_id, lang, message_id=None):
    btn_text = tr(lang, "🔗 Ֆորում", "🔗 Форум", "🔗 Forum")
    msg_text = tr(lang, "💬 Միացիր մեր ֆորումին՝ սեղմիր կոճակը 👇", "💬 Наш форум — жми на кнопку 👇", "💬 Our forum — tap the button 👇")
    forum_btn = types.InlineKeyboardButton(btn_text, url=get_config('forum_link'))
    edit_or_send(chat_id, message_id, card(get_content('btn_forum', lang), msg_text), build_nav_markup(lang, extra_rows=[forum_btn]))


@bot.message_handler(func=lambda m: is_menu_btn(m.text, 'btn_forum'))
def forum(message):
    lang = get_lang(message.chat.id)
    show_typing(message.chat.id)
    hide_main_menu(message.chat.id)
    sec_forum(message.chat.id, lang)


# === IPTV ===
def sec_iptv(chat_id, lang, message_id=None):
    title = get_content('btn_iptv', lang)
    link = get_config('iptv_link')
    if not link:
        edit_or_send(chat_id, message_id, card(title, get_content('text_iptv_missing', lang)), build_nav_markup(lang))
        return
    caption = get_content('text_iptv_caption', lang).format(link=link)
    instructions = get_content('text_iptv_instructions', lang).strip()
    if instructions:
        caption += "\n\n" + instructions
    edit_or_send(chat_id, message_id, card(title, caption), build_nav_markup(lang))


@bot.message_handler(func=lambda m: is_menu_btn(m.text, 'btn_iptv'))
def iptv(message):
    lang = get_lang(message.chat.id)
    show_typing(message.chat.id)
    hide_main_menu(message.chat.id)
    sec_iptv(message.chat.id, lang)


# === ADBLOCK ===
def sec_adblock(chat_id, lang, message_id=None):
    edit_or_send(chat_id, message_id, card(get_content('btn_adblock', lang), get_content('text_adblock', lang)), build_nav_markup(lang))


@bot.message_handler(func=lambda m: is_menu_btn(m.text, 'btn_adblock'))
def adblock_menu(message):
    lang = get_lang(message.chat.id)
    show_typing(message.chat.id)
    hide_main_menu(message.chat.id)
    sec_adblock(message.chat.id, lang)


# === SUPPORT (wizard-style self-help before reaching the admin) ===
def sec_support(chat_id, lang, message_id=None):
    title = tr(lang, "Խելացի Օգնական 🤖", "Умный Помощник 🤖", "Smart Assistant 🤖")
    body = tr(lang,
              "🛠 Խնդրում ենք նշել, թե ինչ խնդրի եք բախվել, որպեսզի արագ լուծենք այն.",
              "🛠 Пожалуйста, укажите, с какой проблемой вы столкнулись, чтобы мы могли быстро её решить.",
              "🛠 Please indicate the issue you are facing so we can resolve it quickly.")
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton(tr(lang, "🔴 Չի միանում", "🔴 Не подключается", "🔴 Can't connect"), callback_data="wizard_connect"),
        types.InlineKeyboardButton(tr(lang, "🐢 Ցածր արագություն", "🐢 Низкая скорость", "🐢 Low speed"), callback_data="wizard_speed"),
        types.InlineKeyboardButton(tr(lang, "✍️ Այլ հարց (Գրել Ադմինին)", "✍️ Другой вопрос (Админу)", "✍️ Other (Contact Admin)"), callback_data="wizard_admin"),
        types.InlineKeyboardButton(get_content("btn_main_menu", lang), callback_data="main_menu"),
    )
    edit_or_send(chat_id, message_id, card(title, body), markup)


@bot.message_handler(func=lambda m: is_menu_btn(m.text, 'btn_support'))
def support(message):
    lang = get_lang(message.chat.id)
    show_typing(message.chat.id)
    hide_main_menu(message.chat.id)
    sec_support(message.chat.id, lang)


@bot.callback_query_handler(func=lambda call: call.data.startswith("wizard_"))
def wizard_router(call):
    lang = get_lang(call.from_user.id)
    bot.answer_callback_query(call.id)
    action = call.data.split('_')[1]
    chat_id = call.message.chat.id
    mid = call.message.message_id
    title = tr(lang, "Խելացի Օգնական 🤖", "Умный Помощник 🤖", "Smart Assistant 🤖")

    if action == "start":
        sec_support(chat_id, lang, mid)
        return

    if action == "connect":
        body = tr(lang,
                  "📱 Ո՞ր հավելվածն եք օգտագործում VPN-ին միանալու համար:",
                  "📱 Какое приложение вы используете для подключения к VPN?",
                  "📱 Which app are you using to connect to the VPN?")
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("Happ", callback_data="wizard_app"),
            types.InlineKeyboardButton("INCY", callback_data="wizard_app"),
        )
        markup.add(
            types.InlineKeyboardButton(get_content("btn_back", lang), callback_data="wizard_start"),
            types.InlineKeyboardButton(get_content("btn_main_menu", lang), callback_data="main_menu"),
        )
        edit_or_send(chat_id, mid, card(title, body), markup)

    elif action == "app":
        body = tr(lang,
                  "🔧 <b>Փորձեք հետևյալ քայլերը.</b>\n\n"
                  "1. Համոզվեք, որ պատճենել եք հենց Ձեր անձնական հղումը:\n"
                  "2. Հավելվածում թարմացրեք (Update) սերվերների ցանկը:\n"
                  "3. Ընտրեք ցանկից մեկ այլ սերվեր (օրինակ 2-րդը) և փորձեք նորից միանալ:\n\n"
                  "Այս քայլերն օգնեցի՞ն լուծել խնդիրը:",
                  "🔧 <b>Попробуйте следующие шаги:</b>\n\n"
                  "1. Убедитесь, что вы скопировали именно вашу личную ссылку.\n"
                  "2. Обновите (Update) список серверов в приложении.\n"
                  "3. Выберите другой сервер из списка (например, 2-й) и попробуйте подключиться снова.\n\n"
                  "Эти шаги помогли решить проблему?",
                  "🔧 <b>Try the following steps:</b>\n\n"
                  "1. Make sure you copied your personal link.\n"
                  "2. Update the server list in the app.\n"
                  "3. Pick a different server from the list (e.g., the 2nd one) and try connecting again.\n\n"
                  "Did these steps help solve the issue?")
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton(tr(lang, "✅ Այո, օգնեց", "✅ Да, помогло", "✅ Yes, it helped"), callback_data="wizard_solved"),
            types.InlineKeyboardButton(tr(lang, "❌ Ոչ (Գրել Ադմինին)", "❌ Нет (Написать Админу)", "❌ No (Contact Admin)"), callback_data="wizard_admin"),
            types.InlineKeyboardButton(get_content("btn_back", lang), callback_data="wizard_connect"),
        )
        edit_or_send(chat_id, mid, card(title, body), markup)

    elif action == "speed":
        body = tr(lang,
                  "⚡ <b>Արագության բարելավում</b>\n\nՍովորաբար դա լուծվում է շատ հեշտ. պարզապես հավելվածում ընտրեք այլ երկրի սերվեր և նորից միացեք:\n\nԱրագությունը բարելավվե՞ց:",
                  "⚡ <b>Улучшение скорости</b>\n\nОбычно это решается очень легко: просто выберите сервер другой страны в приложении и переподключитесь.\n\nСкорость улучшилась?",
                  "⚡ <b>Speed Improvement</b>\n\nThis is usually solved very easily: just select a server from a different country in the app and reconnect.\n\nDid the speed improve?")
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton(tr(lang, "✅ Այո, օգնեց", "✅ Да, помогло", "✅ Yes, it helped"), callback_data="wizard_solved"),
            types.InlineKeyboardButton(tr(lang, "❌ Ոչ (Գրել Ադմինին)", "❌ Нет (Написать Админу)", "❌ No (Contact Admin)"), callback_data="wizard_admin"),
            types.InlineKeyboardButton(get_content("btn_back", lang), callback_data="wizard_start"),
        )
        edit_or_send(chat_id, mid, card(title, body), markup)

    elif action == "solved":
        body = tr(lang,
                  "🎉 Շատ ուրախ ենք, որ խնդիրը լուծվեց: Մաղթում ենք հաճելի օգտագործում:",
                  "🎉 Очень рады, что проблема решена! Желаем приятного пользования.",
                  "🎉 We are glad the issue is resolved! Enjoy using our service.")
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton(get_content("btn_main_menu", lang), callback_data="main_menu"))
        edit_or_send(chat_id, mid, card(title, body), markup)

    elif action == "admin":
        body = get_content('text_support_prompt', lang)
        markup = build_nav_markup(lang, back_callback="wizard_start")
        edit_or_send(chat_id, mid, card(get_content('btn_support', lang), body), markup)


# === TERMS & PRIVACY ===
def sec_info(chat_id, lang, message_id=None):
    edit_or_send(chat_id, message_id, card(get_content('btn_info', lang), get_content('text_info_prompt', lang)), build_info_markup(lang))


@bot.message_handler(func=lambda m: is_menu_btn(m.text, 'btn_info'))
def info_menu(message):
    lang = get_lang(message.chat.id)
    show_typing(message.chat.id)
    hide_main_menu(message.chat.id)
    sec_info(message.chat.id, lang)


@bot.callback_query_handler(func=lambda call: call.data in ("info_terms", "info_privacy"))
def show_info_text(call):
    lang = get_lang(call.from_user.id)
    key = 'text_terms' if call.data == 'info_terms' else 'text_privacy'
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(
            get_content(key, lang),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            disable_web_page_preview=True,
            reply_markup=build_back_markup(lang),
        )
    except Exception:
        bot.send_message(
            call.message.chat.id, get_content(key, lang),
            disable_web_page_preview=True, reply_markup=build_back_markup(lang),
        )


@bot.callback_query_handler(func=lambda call: call.data == "info_back")
def info_back(call):
    lang = get_lang(call.from_user.id)
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(
            get_content('text_info_prompt', lang),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_info_markup(lang),
        )
    except Exception:
        bot.send_message(
            call.message.chat.id, get_content('text_info_prompt', lang),
            reply_markup=build_info_markup(lang),
        )


# === 🎂 Ծննդյան օր ===
@bot.message_handler(commands=['birthday'])
def set_birthday(message):
    """/birthday 25.04 — օգտատերը նշում է ծննդյան օրը, բոտը տարին մեկ շնորհավորում է։"""
    lang = get_lang(message.chat.id)
    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    if arg:
        norm = arg.replace('/', '.').replace('-', '.').replace(',', '.')
        pieces = [p for p in norm.split('.') if p]
        d = m = 0
        try:
            d, m = int(pieces[0]), int(pieces[1])
            datetime(2000, m, d)  # վալիդացիա
        except Exception:
            d = m = 0
        if d and m:
            db_execute("UPDATE users SET birthday = %s, bday_greeted_year = NULL WHERE user_id = %s",
                       (f"{d:02d}.{m:02d}", message.chat.id), commit=True)
            bot.send_message(message.chat.id, tr(lang,
                f"🎂 Հիշեցի՝ {d:02d}.{m:02d}։ Այդ օրը անակնկալ կլինի 😉",
                f"🎂 Запомнил: {d:02d}.{m:02d}. Жди сюрприз в этот день 😉",
                f"🎂 Got it: {d:02d}.{m:02d}. Expect a surprise that day 😉"))
            return
    bot.send_message(message.chat.id, tr(lang,
        "🎂 Գրիր ծննդյանդ օրը այսպես՝ <code>/birthday 25.04</code> (օր.ամիս)",
        "🎂 Укажи день рождения так: <code>/birthday 25.04</code> (день.месяц)",
        "🎂 Set your birthday like this: <code>/birthday 25.04</code> (day.month)"), parse_mode="HTML")


# === FEEDBACK (⭐ Գնահատիր բոտը) ===
def fb_escape(s):
    """Օգտատիրոջ տեքստի HTML escape՝ parse_mode=HTML-ի համար։"""
    return (s or "").replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def pop_pending_feedback(user_id, max_age=900):
    """True, եթե օգտատերը վերջին 15 րոպեում գնահատել է ու սպասում ենք մեկնաբանության։"""
    row = db_execute("SELECT ts FROM pending_feedback WHERE user_id = %s", (user_id,), fetchone=True)
    if not row:
        return False
    db_execute("DELETE FROM pending_feedback WHERE user_id = %s", (user_id,), commit=True)
    return (time.time() - row[0]) <= max_age


def build_rate_markup(lang, current=0):
    markup = types.InlineKeyboardMarkup()
    stars = [types.InlineKeyboardButton(("⭐" if i <= current else "☆") + str(i),
                                        callback_data=f"rate_{i}") for i in range(1, 6)]
    markup.row(*stars)
    markup.add(types.InlineKeyboardButton(
        tr(lang, "💬 Կարծիքներ", "💬 Отзывы", "💬 Reviews"),
        callback_data="menu_reviews"))
    markup.add(types.InlineKeyboardButton(get_content("btn_main_menu", lang), callback_data="main_menu"))
    return markup


def sec_rate(chat_id, lang, message_id=None):
    """⭐ Գնահատիր բոտը բաժինը՝ 1-5 աստղ, in-place։"""
    row = db_execute("SELECT rating FROM feedback WHERE user_id = %s", (chat_id,), fetchone=True)
    current = row[0] if row and row[0] else 0
    stats = db_execute("SELECT COUNT(*), AVG(rating) FROM feedback WHERE rating IS NOT NULL", fetchone=True)
    total = stats[0] if stats and stats[0] else 0
    avg_line = ""
    if total:
        avg = float(stats[1] or 0)
        avg_line = tr(lang,
                      f"🌟 Միջին գնահատականը՝ <b>{avg:.1f}/5</b> ({total} ձայն)\n\n",
                      f"🌟 Средняя оценка: <b>{avg:.1f}/5</b> ({total} гол.)\n\n",
                      f"🌟 Average rating: <b>{avg:.1f}/5</b> ({total} votes)\n\n")
    title = tr(lang, "Գնահատիր բոտը", "Оцени бота", "Rate the bot")
    if current:
        body = tr(lang,
                  f"Քո գնահատականը՝ {'⭐' * current} ({current}/5)։ Կարող ես փոխել այն 👇",
                  f"Твоя оценка: {'⭐' * current} ({current}/5). Можешь изменить её 👇",
                  f"Your rating: {'⭐' * current} ({current}/5). You can change it 👇")
    else:
        body = tr(lang,
                  "Որքանո՞վ ես գոհ բոտից։ Ընտրիր 1-ից 5 աստղ 👇",
                  "Насколько тебе нравится бот? Выбери от 1 до 5 звёзд 👇",
                  "How do you like the bot? Pick 1 to 5 stars 👇")
    edit_or_send(chat_id, message_id, card(title, avg_line + body), build_rate_markup(lang, current))


def sec_reviews(chat_id, lang, message_id=None):
    """💬 Հրապարակային կարծիքներ. բոլորը տեսնում են գնահատականներն ու մեկնաբանությունները։"""
    stats = db_execute("SELECT COUNT(*), AVG(rating) FROM feedback WHERE rating IS NOT NULL", fetchone=True)
    total = stats[0] if stats and stats[0] else 0
    title = tr(lang, "Կարծիքներ", "Отзывы", "Reviews")
    if not total:
        body = tr(lang,
                  "Դեռ կարծիքներ չկան։ Եղիր առաջինը 👇",
                  "Отзывов пока нет. Будь первым 👇",
                  "No reviews yet. Be the first 👇")
        edit_or_send(chat_id, message_id, card(title, body),
                     build_nav_markup(lang, back_callback="menu_rate"))
        return
    avg = float(stats[1] or 0)
    lines = [tr(lang,
                f"🌟 Միջին գնահատականը՝ <b>{avg:.1f}/5</b> ({total} ձայն)",
                f"🌟 Средняя оценка: <b>{avg:.1f}/5</b> ({total} гол.)",
                f"🌟 Average rating: <b>{avg:.1f}/5</b> ({total} votes)"), ""]
    rows = db_execute(
        "SELECT name, rating, comment, reply FROM feedback "
        "WHERE comment IS NOT NULL AND comment <> '' "
        "ORDER BY updated_at DESC LIMIT 10", fetchall=True) or []
    if rows:
        for rv_name, rv_rating, rv_comment, rv_reply in rows:
            who = fb_escape((rv_name or "").strip()) or tr(lang, "Օգտատեր", "Пользователь", "User")
            lines.append(f"{'⭐' * (rv_rating or 0)} <b>{who}</b>")
            lines.append(f"«{fb_escape(rv_comment[:200])}»")
            if rv_reply:
                reply_label = tr(lang, "↩️ VedaVPN-ի պատասխանը", "↩️ Ответ VedaVPN", "↩️ Reply from VedaVPN")
                lines.append(f"<i>{reply_label}. {fb_escape(rv_reply[:200])}</i>")
            lines.append("")
    else:
        lines.append(tr(lang,
                        "Մեկնաբանություններ դեռ չկան, միայն գնահատականներ 🙂",
                        "Комментариев пока нет, только оценки 🙂",
                        "No comments yet, only ratings 🙂"))
    edit_or_send(chat_id, message_id, card(title, "\n".join(lines).strip()),
                 build_nav_markup(lang, back_callback="menu_rate"))


@bot.callback_query_handler(func=lambda call: call.data.startswith("rate_"))
def rate_callback(call):
    """Աստղի սեղմում. պահպանում ենք գնահատականը և առաջարկում մեկնաբանություն թողնել։"""
    lang = get_lang(call.from_user.id)
    try:
        rating = max(1, min(5, int(call.data[5:])))
    except (ValueError, TypeError):
        bot.answer_callback_query(call.id)
        return
    db_execute(
        "INSERT INTO feedback (user_id, rating, name, updated_at) VALUES (%s, %s, %s, now()) "
        "ON CONFLICT (user_id) DO UPDATE SET rating = EXCLUDED.rating, name = EXCLUDED.name, updated_at = now()",
        (call.from_user.id, rating, (call.from_user.first_name or "").strip()[:64]), commit=True
    )
    db_execute(
        "INSERT INTO pending_feedback (user_id, ts) VALUES (%s, %s) "
        "ON CONFLICT (user_id) DO UPDATE SET ts = EXCLUDED.ts",
        (call.from_user.id, time.time()), commit=True
    )
    bot.answer_callback_query(call.id, "⭐" * rating)
    title = tr(lang, "Շնորհակալություն 🙏", "Спасибо 🙏", "Thank you 🙏")
    body = tr(lang,
              f"Գնահատականդ պահպանված է՝ {'⭐' * rating} ({rating}/5)։\n\nՑանկության դեպքում գրիր մեկ հաղորդագրությամբ, թե ինչ բարելավենք — այն կհասնի ադմինին։",
              f"Оценка сохранена: {'⭐' * rating} ({rating}/5).\n\nЕсли хочешь, напиши одним сообщением, что улучшить — оно попадёт к админу.",
              f"Your rating is saved: {'⭐' * rating} ({rating}/5).\n\nIf you like, send one message with what to improve — it will reach the admin.")
    edit_or_send(call.message.chat.id, call.message.message_id, card(title, body),
                 build_nav_markup(lang, back_callback="menu_rate"))


@bot.callback_query_handler(func=lambda call: call.data == "main_menu")
def go_main_menu(call):
    """Ցանկացած inline բաժնից վերադարձ գլխավոր մենյու՝ in-place։"""
    lang = get_lang(call.from_user.id)
    bot.answer_callback_query(call.id)
    send_main_menu(call.message.chat.id, lang, message_id=call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("menu_"))
def menu_router(call):
    """Inline գլխավոր մենյուի բաժինների router՝ in-place նավիգացիայով։"""
    lang = get_lang(call.from_user.id)
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    mid = call.message.message_id
    key = call.data[5:]
    sections = {
        "vpn": sec_vpn, "refs": sec_refs, "howto": sec_howto, "faq": sec_faq,
        "support": sec_support, "forum": sec_forum, "iptv": sec_iptv, "info": sec_info,
        "adblock": sec_adblock, "rate": sec_rate, "reviews": sec_reviews,
    }
    if key in sections:
        sections[key](chat_id, lang, mid)
        return
    if key.startswith("cbtn_"):
        try:
            row = db_execute(
                "SELECT label_hy, label_ru, response_hy, response_ru FROM custom_buttons WHERE id = %s",
                (int(key[5:]),), fetchone=True
            )
        except Exception:
            row = None
        if row:
            label = row[0] if lang == 'hy' else row[1]
            resp = (row[2] if lang == 'hy' else row[3]) or ""
            edit_or_send(chat_id, mid, card(label, resp), build_nav_markup(lang))


# === ADMIN. Reply to a review ===
@bot.message_handler(commands=['freply'])
def feedback_reply(message):
    """/freply <user_id> <տեքստ> — ադմինի պատասխան կարծիքին. երևում է «Կարծիքներ» բաժնում և ուղարկվում հեղինակին։"""
    if message.chat.id != ADMIN_ID:
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        bot.send_message(ADMIN_ID, "Օգտագործում՝ <code>/freply user_id պատասխանի տեքստ</code>", parse_mode="HTML")
        return
    fb_uid = int(parts[1])
    reply_text = parts[2].strip()[:500]
    row = db_execute("SELECT rating, comment FROM feedback WHERE user_id = %s", (fb_uid,), fetchone=True)
    if not row:
        bot.send_message(ADMIN_ID, "Այդ ID-ով կարծիք չկա։")
        return
    db_execute("UPDATE feedback SET reply = %s WHERE user_id = %s", (reply_text, fb_uid), commit=True)
    user_lang = get_lang(fb_uid)
    notice = tr(user_lang,
                f"↩️ VedaVPN-ը պատասխանել է քո կարծիքին․\n\n«{fb_escape(reply_text)}»",
                f"↩️ VedaVPN ответил на твой отзыв:\n\n«{fb_escape(reply_text)}»",
                f"↩️ VedaVPN replied to your review:\n\n«{fb_escape(reply_text)}»")
    try:
        bot.send_message(fb_uid, notice)
        bot.send_message(ADMIN_ID, "✅ Պատասխանը պահպանվեց և ուղարկվեց հեղինակին։")
    except Exception:
        bot.send_message(ADMIN_ID, "✅ Պատասխանը պահպանվեց, բայց հեղինակին ուղարկել չստացվեց (հնարավոր է՝ բլոկել է բոտը)։")


# === ADMIN. Feedback stats ===
@bot.message_handler(commands=['feedback_stats'])
def feedback_stats(message):
    if message.chat.id != ADMIN_ID:
        return
    rows = db_execute("SELECT rating, COUNT(*) FROM feedback GROUP BY rating", fetchall=True) or []
    total = sum(r[1] for r in rows)
    if not total:
        bot.send_message(ADMIN_ID, "⭐ Դեռ գնահատականներ չկան։")
        return
    avg = sum((r[0] or 0) * r[1] for r in rows) / total
    counts = {r[0]: r[1] for r in rows}
    lines = ["⭐ <b>Feedback վիճակագրություն</b>",
             f"Միջին գնահատական՝ <b>{avg:.2f}/5</b> ({total} ձայն)", ""]
    for star in range(5, 0, -1):
        n = counts.get(star, 0)
        bar = "▮" * min(n, 20) + ("…" if n > 20 else "")
        lines.append(f"{star}★ — {n} {bar}")
    comments = db_execute(
        "SELECT user_id, rating, comment FROM feedback "
        "WHERE comment IS NOT NULL AND comment <> '' "
        "ORDER BY updated_at DESC LIMIT 10", fetchall=True) or []
    if comments:
        lines.append("")
        lines.append("💬 <b>Վերջին մեկնաբանությունները</b>")
        for fb_uid, fb_r, fb_c in comments:
            lines.append(f"• <code>{fb_uid}</code> {'⭐' * (fb_r or 0)} — {fb_escape(fb_c[:150])}")
        lines.append("")
        lines.append("↩️ Պատասխանելու համար. <code>/freply user_id տեքստ</code>")
    bot.send_message(ADMIN_ID, "\n".join(lines), parse_mode="HTML")


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
        f"/stats — մանրամասն վիճակագրություն 📊\n"
        f"/growth — վերջին 30 օրվա աճի գրաֆիկ 📈\n"
        f"/export — օգտատերերի ցուցակը CSV ֆայլով 📁\n"
        f"/ban ID — անջատել օգտատիրոջ sub հղումը 🚫\n"
        f"/unban ID — վերականգնել օգտատիրոջ sub հղումը ✅\n"
        f"/broadcast տեքստ (կամ նկար/վիդեո՝ caption-ում /broadcast տեքստ) — ուղարկել բոլորին\n"
        f"/reply ID տեքստ — պատասխանել user-ի\n"
        f"/listkeys — ��ույց տալ բոլոր editable content key-ները\n"
        f"/getcontent key — ցույց տալ key-ի ընթացիկ hy/ru արժեքները\n"
        f"/setcontent key lang տեքստ — փոխել կոճակի/տեքստի արժեքը\n"
        f"/getconfig — ցույց տալ VPN/forum հղումները\n"
        f"/setlink նոր_հղում — փոխել VPN հղումը\n"
        f"/setforum նոր_հղում — փոխել forum հղումը\n"
        f"/setiptv նոր_հղում — փոխել IPTV հղումը\n"
        f"/setphoto — սահմանել ողջյունի նկարը (ուղարկիր նկար caption-ում /setphoto, կամ reply արա նկարին)\n\n"
        f"<b>Նոր կոճակներ.</b>\n"
        f"/addbutton անուն_hy|անուն_ru|պատասխան_hy|պատասխան_ru — ավելացնել նոր կոճակ (առա��ց prefix, պարզապես 4 մաս | -ով)\n"
        f"/listbuttons — ցույց տալ ավելացված կոճակները\n"
        f"/removebutton ID — հեռացնել կոճակ\n\n"
        f"<b>Sub ֆայլի կառավարում (GitHub).</b>\n"
        f"/update_sub [տեքստ] — թա������ացնել ամբողջ sub ֆայլը\n"
        f"/append_sub [հղում] — ավելացնել նոր սերվեր (առանց ջնջելու)\n"
        f"/delete_sub_keyword [հիմնաբառ] — ջնջել սերվերը\n"
        f"/list_and_delete — ցույց տալ սերվերները կոճակներով\n"
        f"/show_sub — ցույց տալ ֆայլի բովանդակությունը\n"
        f"/clear_sub — ջնջել բոլոր սերվերները"
    )


@bot.message_handler(commands=['stats'])
def stats_cmd(message):
    """Ընդլայնված վիճակագրություն ադմինի համար։"""
    if message.chat.id != ADMIN_ID:
        return

    def scalar(query):
        row = db_execute(query, fetchone=True)
        return (row[0] if row and row[0] is not None else 0)

    total = scalar("SELECT COUNT(*) FROM users")
    today = scalar("SELECT COUNT(*) FROM users WHERE created_at >= CURRENT_DATE")
    week = scalar("SELECT COUNT(*) FROM users WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'")
    hy = scalar("SELECT COUNT(*) FROM users WHERE lang = 'hy'")
    ru = scalar("SELECT COUNT(*) FROM users WHERE lang = 'ru'")
    en = scalar("SELECT COUNT(*) FROM users WHERE lang = 'en'")
    banned_cnt = scalar("SELECT COUNT(*) FROM users WHERE banned")
    android = scalar("SELECT COUNT(*) FROM users WHERE device = 'android'")
    ios = scalar("SELECT COUNT(*) FROM users WHERE device = 'ios'")
    total_refs = scalar("SELECT COALESCE(SUM(ref_count), 0) FROM users")
    top = db_execute(
        "SELECT user_id, ref_count FROM users WHERE ref_count > 0 ORDER BY ref_count DESC LIMIT 5",
        fetchall=True
    ) or []

    def pct(n):
        return f"{n * 100 // total}%" if total else "0%"

    lines = [
        "📊 <b>Ընդլայնված վիճակագրություն</b>",
        "",
        f"👥 Ընդհանուր օգտատերեր՝ <b>{total}</b>",
        f"🆕 Այսօր՝ <b>{today}</b>",
        f"📅 Վերջին 7 օրը՝ <b>{week}</b>",
        "",
        "<b>🌍 Լեզուներ</b>",
        f"🇦🇲 Հայ��րեն՝ {hy} ({pct(hy)})",
        f"🇷🇺 Русский՝ {ru} ({pct(ru)})",
        f"🇬🇧 English՝ {en} ({pct(en)})",
        "",
        "<b>📱 Սարքեր</b>",
        f"🤖 Android՝ {android}",
        f"🍏 iPhone՝ {ios}",
        "",
        f"🔗 Ընդհանուր ռեֆերալներ՝ <b>{total_refs}</b>",
        f"🚫 Անջատված sub հղումներ՝ <b>{banned_cnt}</b>",
    ]
    if top:
        lines.append("")
        lines.append("<b>🏆 Թոփ հրավիրողներ</b>")
        for i, (uid, cnt) in enumerate(top, 1):
            lines.append(f"{i}. <code>{uid}</code> — {cnt}")

    bot.send_message(ADMIN_ID, "\n".join(lines), parse_mode="HTML")


@bot.message_handler(commands=['export'])
def export_users_cmd(message):
    """Օգտատերերի ամբողջ ցուցակը CSV ֆայլով՝ ադմինի համար։"""
    if message.chat.id != ADMIN_ID:
        return
    rows = db_execute(
        "SELECT user_id, lang, ref_by, ref_count, device, created_at, banned, sub_fetches, last_sub_at FROM users ORDER BY created_at",
        fetchall=True
    ) or []
    from io import StringIO
    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(["user_id", "lang", "ref_by", "ref_count", "device", "created_at", "banned", "sub_fetches", "last_sub_at"])
    for r in rows:
        writer.writerow(r)
    bio = BytesIO(buf.getvalue().encode('utf-8-sig'))
    bio.name = "vedavpn_users.csv"
    bot.send_document(ADMIN_ID, bio, caption=f"📁 Ընդամենը {len(rows)} օգտատեր")


@bot.message_handler(commands=['growth'])
def growth_cmd(message):
    """Վերջին 30 օրվա նոր գրանցումների տեքստային գրաֆիկ։"""
    if message.chat.id != ADMIN_ID:
        return
    rows = db_execute(
        "SELECT created_at::date AS d, COUNT(*) FROM users "
        "WHERE created_at >= CURRENT_DATE - INTERVAL '29 days' "
        "GROUP BY d ORDER BY d",
        fetchall=True
    ) or []
    counts = {r[0]: r[1] for r in rows}
    today = date.today()
    days = [today - timedelta(days=i) for i in range(29, -1, -1)]
    mx = max([counts.get(d, 0) for d in days] + [1])
    lines = ["📈 <b>Նոր օգտատերեր՝ վերջին 30 օր</b>", ""]
    total = 0
    for d in days:
        c = counts.get(d, 0)
        total += c
        bar = "▇" * (max(1, round(c / mx * 12)) if c else 0)
        lines.append(f"<code>{d.strftime('%d.%m')} {bar or '·'} {c}</code>")
    lines.append("")
    lines.append(f"Ընդամենը՝ <b>{total}</b>")
    bot.send_message(ADMIN_ID, "\n".join(lines), parse_mode="HTML")


@bot.message_handler(commands=['ban'])
def ban_cmd(message):
    """Անջատում է օգտատիրոջ անհատական sub հղումը։"""
    if message.chat.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.send_message(ADMIN_ID, "Օգտագործում՝ <code>/ban USER_ID</code>")
        return
    uid = int(parts[1])
    db_execute("UPDATE users SET banned = TRUE WHERE user_id = %s", (uid,), commit=True)
    bot.send_message(ADMIN_ID, f"🚫 <code>{uid}</code> օգտատիրոջ sub հղումն անջատվեց")


@bot.message_handler(commands=['unban'])
def unban_cmd(message):
    """Վերականգնում է օգտատիրոջ անհատական sub հղումը։"""
    if message.chat.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.send_message(ADMIN_ID, "Օգտագործում՝ <code>/unban USER_ID</code>")
        return
    uid = int(parts[1])
    db_execute("UPDATE users SET banned = FALSE WHERE user_id = %s", (uid,), commit=True)
    bot.send_message(ADMIN_ID, f"✅ <code>{uid}</code> օգտատիրոջ sub հղումը վերականգնվեց")


def _do_broadcast(message):
    if message.chat.id != ADMIN_ID:
        return

    photo_id = message.photo[-1].file_id if message.photo else None
    video_id = message.video.file_id if message.video else None

    if photo_id or video_id:
        parts = (message.caption or '').strip().split(maxsplit=1)
        text = parts[1] if len(parts) > 1 else ""
    else:
        try:
            text = message.text.split(maxsplit=1)[1]
        except IndexError:
            bot.send_message(
                ADMIN_ID,
                "❌ Օգտագործիր՝\n"
                "<code>/broadcast տեքստ</code>\n"
                "կամ ուղարկիր նկար/վիդեո՝ caption-ում գրելով.\n"
                "<code>/broadcast տեքստ</code>"
            )
            return

    users = db_execute("SELECT user_id FROM users", fetchall=True) or []
    # Յուրաքանչյուր user-ի համար հերթի մեջ գրանցում ենք DB-ում (ոչ միայն հիշողությունում),
    # որպեսզի Render-ի restart/sleep-ի դեպքում էլ broadcast-ը կիսատ չմնա. գործարկվելուն
    # պես _broadcast_worker_db-ն ինքն է վերսկսում մնացած հերթը։
    for (uid,) in users:
        db_execute(
            "INSERT INTO broadcast_queue (user_id, text, photo_id, video_id) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (user_id) DO NOTHING",
            (uid, text, photo_id, video_id), commit=True
        )
    threading.Thread(target=_broadcast_worker_db, daemon=True).start()
    bot.send_message(ADMIN_ID, f"📤 Broadcast-ը գրանցվեց և սկսվեց՝ {len(users)} օգտատեր։ Ընդհատվելու դեպքում այն ինքնաշխատ կշարունակվի միանալուն պես։")


def _broadcast_send(uid, text, photo_id, video_id):
    if photo_id:
        bot.send_photo(uid, photo_id, caption=text or None)
    elif video_id:
        bot.send_video(uid, video_id, caption=text or None)
    else:
        bot.send_message(uid, text)


def _broadcast_worker_db():
    """DB-ից մեկ առ մեկ վերցնում ու ուղարկում է հերթի հաղորդագրությունները։
    Քանի որ state-ը (ում ուղարկվեց, ում՝ ոչ) պահվում է հենց DB-ում, restart-ից
    հետո էլ գործարկվելիս ինքն է շարունակում այնտեղից, որտեղ կանգ էր առել։"""
    sent = 0
    failed = 0
    while True:
        row = db_execute("SELECT user_id, text, photo_id, video_id FROM broadcast_queue LIMIT 1", fetchone=True)
        if not row:
            break
        uid, text, photo_id, video_id = row
        try:
            _broadcast_send(uid, text, photo_id, video_id)
            db_execute("DELETE FROM broadcast_queue WHERE user_id = %s", (uid,), commit=True)
            sent += 1
        except ApiTelegramException as e:
            # Rate limit (429)՝ սպասում ենք Telegram-ի ասած ժամանակը և կրկնում (տողը մնում է հերթում)
            if e.error_code == 429:
                retry_after = 1
                try:
                    retry_after = int(e.result_json.get('parameters', {}).get('retry_after', 1))
                except Exception:
                    pass
                time.sleep(retry_after + 1)
                continue
            else:
                db_execute("DELETE FROM broadcast_queue WHERE user_id = %s", (uid,), commit=True)
                failed += 1
        except Exception:
            db_execute("DELETE FROM broadcast_queue WHERE user_id = %s", (uid,), commit=True)
            failed += 1
        # ~20 հաղորդագրություն/վայրկյան՝ Telegram-ի սահմանաչափից ցածր
        time.sleep(0.05)
    if sent > 0 or failed > 0:
        try:
            bot.send_message(ADMIN_ID, f"✅ Broadcast-ի ցիկլն ավարտվեց՝ նոր ուղարկված {sent}, ձախողված {failed}")
        except Exception:
            log.exception("broadcast summary failed")


@bot.message_handler(commands=['broadcast'])
def broadcast(message):
    _do_broadcast(message)


@bot.message_handler(
    content_types=['photo'],
    func=lambda m: m.chat.id == ADMIN_ID and m.caption and m.caption.strip().split()[0] == '/broadcast'
)
def broadcast_with_photo(message):
    _do_broadcast(message)


@bot.message_handler(
    content_types=['video'],
    func=lambda m: m.chat.id == ADMIN_ID and m.caption and m.caption.strip().split()[0] == '/broadcast'
)
def broadcast_with_video(message):
    _do_broadcast(message)


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
        bot.send_message(ADMIN_ID, f"❌ ��նհայտ key. Տես /listkeys")
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
            "<code>/addbutton 🔒 Անվ��անգություն|🔒 Безопасность|Երբեք մի օգտագործեք VPN-ը հանրա��ին WiFi-ում առանց...|Ник��гда не используйте VPN в публичном WiFi без...</code>"
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
        bot.send_message(ADMIN_ID, "ℹ️ Ավել��ցված custom կոճակներ չկան։")
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
    bot.send_message(ADMIN_ID, f"✅ Կոճակ {button_id} հ��ռացվեց։")


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
                name = unquote(parts[-1].strip())
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
                name = unquote(line.split('#')[-1].strip())
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
        bot.answer_callback_query(call.id, "Միայ�� ադմինը կարող է:")
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
    bot.send_message(ADMIN_ID, "⚠️ Դուք պատրաստվում եք ջնջել ԲՈԼՈՐ սերվ��ր��երը։\nՀամոզվա՞ծ եք։", reply_markup=markup)


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


# Բոլոր մենյուի կոճակների key-երը, որոնց տեքստը երբեք չպետք է
# փոխանցվի աջակցությանը որպես "նոր հաղորդագրություն"
MENU_BUTTON_KEYS = [
    'btn_get_vpn', 'btn_referrals', 'btn_howto', 'btn_faq',
    'btn_support', 'btn_forum', 'btn_iptv', 'btn_info',
    'btn_terms', 'btn_privacy',
]


def is_menu_button(text):
    if not text:
        return False
    for key in MENU_BUTTON_KEYS:
        if text in (get_content(key, 'hy'), get_content(key, 'ru'), get_content(key, 'en')):
            return True
    row = db_execute(
        "SELECT 1 FROM custom_buttons WHERE label_hy = %s OR label_ru = %s",
        (text, text), fetchone=True
    )
    return row is not None


# === ԱՎՏՈՄԱՏ FAQ ՈՐՈՆՈՒՄ (auto-answer before forwarding to admin) ===
# Երբ օգտատերը ազատ տեքստով հարց է գրում, բոտը նախ փորձում է գտնել
# համապատասխան պատրաստի պատասխան՝ ըստ հիմնաբառերի, և միայն դրանից հետո
# (եթե օգտատերը սեղմի «Դեռ գրել ադմինին») հաղորդագրությունը փոխանցվում է ադմինին։
FAQ_SEARCH = [
    {
        'keywords': ['չի աշխատ', 'չաշխատ', 'չի միանում', 'չի բացվում', 'error', 'սխալ',
                     'проблем', 'не работает', 'не подключ', 'не открыв', 'ошибк', 'глючит', 'тормоз'],
        'hy': "🔧 Եթե VPN-ը չի աշխատում՝ փորձիր ընտրել այլ սերվեր հավելվածում (Happ/INCY)։ "
              "Հաճախ մեկ սերվերը կարող է ժամանակավորապես անհասանելի լինել, իսկ մյուսները՝ աշխատել։ "
              "Նաև ստուգիր, որ դեռ բաժանորդագրված ես ալիքին։",
        'ru': "🔧 Если VPN не работает — попробуйте выбрать другой сервер в приложении (Happ/INCY)։ "
              "Часто один сервер может быть временно недоступен, а другие работают։ "
              "Также проверьте, что вы всё ещё подписаны на канал։",
    },
    {
        'keywords': ['ինչպե�� տեղադր', 'ինչպես ավելացն', 'ինչպես միացն', 'տեղադր', 'կարգավոր', 'ինստալ',
                     'как установ', 'как добав', 'как подключ', 'как настро', 'установить', 'инструкц'],
        'hy': "📖 Տեղադրման քայլ առ քայլ ցուցումների համար սեղմիր «📖 Ինչպես տեղադրել» կոճակը menu-ից։",
        'ru': "📖 Пошаговая инс��рукция по установке — нажмите кнопку «📖 Как установить» в меню։",
    },
    {
        'keywords': ['վճար', 'գին', 'արժ', 'ինչքան', 'փող', 'անվճար',
                     'платн', 'цен', 'стоит', 'сколько', 'деньг', 'бесплатн', 'о��лат'],
        'hy': "💰 VPN-ը ներկ��յումս ամբողջությամբ անվճար է մեր ալիքի բաժանորդների համար։",
        'ru': "💰 VPN сейчас п��лностью бесплатный для подписчиков нашего канала։",
    },
    {
        'keywords': ['հղո��մ', '��տանալ vpn', 'որտեղ vpn', 'sub', 'подписк', 'ссылк', 'получить vpn', 'где vpn'],
        'hy': "🔗 VPN հղումը ստանալու համար սեղմիր «🛡 Ստանալ VPN» կոճակը, բաժանորդագրվիր ալիքին, "
              "և հղումը կո��ղարկվի ավ��ոմատ։",
        'ru': "🔗 Чтобы получить VPN-ссылку, нажмите «🛡 Получить VPN», подпишитесь на канал — "
              "ссылка придёт автоматически։",
    },
    {
        'keywords': ['iptv', 'հեռուստ', 'ալիք դիտ', 'канал', 'телевид', 'тв '],
        'hy': "📺 IPTV դիտելու համար սեղմիր «📺 IPTV» կոճակը menu-ից։",
        'ru': "📺 Для IPTV нажмите кнопку «📺 IPTV» в меню։",
    },
    {
        'keywords': ['ռեֆերալ', 'հրավ', 'ընկեր', 'бонус', 'реферал', 'пригласить', 'друз', 'bonus'],
        'hy': "👥 Ընկերներ հրավիրելու համար բացիր «👥 Ռեֆերալներ» բաժինը՝ այնտեղ քո անձնական հղումն է։",
        'ru': "👥 Чтобы приглашать друзей, откройте раздел «👥 Рефералы» — там ваша персональная ссылка։",
    },
]


def find_faq_answer(text, lang):
    if not text:
        return None
    low = text.lower()
    for item in FAQ_SEARCH:
        for kw in item['keywords']:
            if kw in low:
                return item.get(lang) or item.get('ru')
    return None


# Ժամանակավոր պահոց՝ FAQ-ի ավտոպատասխանից հետո ադմինին փոխանցելու համար։
# Պահվում է DB-ում, որ gunicorn-ի մի քանի worker-ների դեպքում էլ աշխատի��
def set_pending_support(user_id, from_user_id, username, chat_id, message_id):
    db_execute(
        "INSERT INTO pending_support (user_id, from_user_id, username, chat_id, message_id, ts) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (user_id) DO UPDATE SET from_user_id = EXCLUDED.from_user_id, "
        "username = EXCLUDED.username, chat_id = EXCLUDED.chat_id, "
        "message_id = EXCLUDED.message_id, ts = EXCLUDED.ts",
        (user_id, from_user_id, username, chat_id, message_id, time.time()), commit=True
    )


def pop_pending_support(user_id):
    row = db_execute(
        "SELECT from_user_id, username, chat_id, message_id, ts FROM pending_support WHERE user_id = %s",
        (user_id,), fetchone=True
    )
    if not row:
        return None
    db_execute("DELETE FROM pending_support WHERE user_id = %s", (user_id,), commit=True)
    return {'user_id': row[0], 'username': row[1], 'chat_id': row[2], 'message_id': row[3], 'ts': row[4]}


# === ALL OTHER MESSAGES → FORWARDED TO ADMIN, with ID and profile link ===
@bot.message_handler(func=lambda m: True)
def forward_to_admin(message):
    if message.chat.id == ADMIN_ID:
        return
    if is_menu_button(message.text):
        return

    # ⭐ Feedback. եթե օգտատերը նոր է գնահատել, հաջորդ հաղորդագրությունը մեկնաբանություն է
    if pop_pending_feedback(message.chat.id):
        fb_lang = get_lang(message.chat.id)
        comment = (message.text or "").strip()[:1000]
        if comment:
            db_execute(
                "UPDATE feedback SET comment = %s, updated_at = now() WHERE user_id = %s",
                (comment, message.chat.id), commit=True
            )
            fb_row = db_execute("SELECT rating FROM feedback WHERE user_id = %s",
                                (message.chat.id,), fetchone=True)
            fb_rating = fb_row[0] if fb_row and fb_row[0] else 0
            fb_user = message.from_user
            fb_username = f"@{fb_user.username}" if fb_user.username else "(username չկա)"
            try:
                bot.send_message(
                    ADMIN_ID,
                    f"⭐ <b>Նոր feedback</b>\n"
                    f"👤 ID: <code>{fb_user.id}</code> {fb_username}\n"
                    f"Գնահատական՝ {'⭐' * fb_rating} ({fb_rating}/5)\n\n"
                    f"💬 {fb_escape(comment)}",
                    parse_mode="HTML"
                )
            except Exception:
                log.exception("feedback notify failed")
            bot.send_message(message.chat.id, tr(fb_lang,
                "🙏 Շնորհակալություն, կարծիքդ փոխանցվեց ադմինին։",
                "🙏 Спасибо, твой отзыв передан админу.",
                "🙏 Thanks, your feedback was sent to the admin."))
        return

    # Ավտոմատ FAQ որոնում. եթե գտնվի համապատասխան պատասխան, առաջարկում ենք այն
    # և ադմինին ուղարկում միայն օգտատիրոջ հատուկ խնդրանքով։
    lang = get_lang(message.chat.id)
    faq_answer = find_faq_answer(message.text, lang)
    if faq_answer:
        set_pending_support(
            message.chat.id,
            message.from_user.id,
            message.from_user.username,
            message.chat.id,
            message.message_id,
        )
        intro = tr(lang, "💡 Հնարավոր է սա օգնի քո հարցին՝", "💡 Возможно, это ответит на ваш вопрос:", "💡 This might answer your question:")
        btn = tr(lang, "🆘 Դեռ գրել ադմինին", "🆘 Всё равно написать в поддержку", "🆘 Contact support anyway")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(btn, callback_data="force_support"))
        bot.send_message(message.chat.id, f"{intro}\n\n{faq_answer}", reply_markup=markup)
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
        bot.copy_message(ADMIN_ID, message.chat.id, message.message_id)
    except Exception:
        log.exception("forward to admin failed")


@bot.callback_query_handler(func=lambda call: call.data == "force_support")
def force_support(call):
    """Երբ FAQ-ը բավարար չէ՝ օգտատերը այս կոճակով կարող է հաղորդագրությունը ուղարկել ադմինին։"""
    lang = get_lang(call.from_user.id)
    bot.answer_callback_query(call.id)
    pending = pop_pending_support(call.from_user.id)
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    if not pending or time.time() - pending['ts'] > 3600:
        bot.send_message(call.from_user.id, get_content('text_support_prompt', lang))
        return
    uid = pending['user_id']
    uname = pending.get('username')
    username = f"@{uname}" if uname else ("(username չկա)" if lang == 'hy' else "(нет username)")
    profile_link = f"https://t.me/{uname}" if uname else f"tg://user?id={uid}"
    info = (
        f"✉️ <b>Նոր հաղորդագրություն (FAQ-ից հետո)</b>\n"
        f"👤 <b>ID:</b> <code>{uid}</code>\n"
        f"👤 <b>Username:</b> {username}\n"
        f"🔗 <a href=\"{profile_link}\">Profile-ը բացել</a>\n\n"
        f"↩️ Պատասխանելու համար. <code>/reply {uid} ��եքստ</code>"
    )
    try:
        bot.send_message(ADMIN_ID, info, parse_mode="HTML", disable_web_page_preview=True)
        bot.copy_message(ADMIN_ID, pending['chat_id'], pending['message_id'])
        done = "✅ Ուղարկվեց ադմինին, շուտով կպատասխանեն։" if lang == 'hy' else "✅ Отправлено администратору, скоро ответят։"
        bot.send_message(call.from_user.id, done)
    except Exception:
        log.exception("force support forward failed")


# === FLASK WEB APP + WEBHOOK ===
@app.route(WEBHOOK_PATH, methods=['POST'])
def webhook():
    # Ստուգում ��նք, որ request-ը իսկապես Telegram-ից է (secret token)
    if request.headers.get('X-Telegram-Bot-Api-Secret-Token') != WEBHOOK_SECRET:
        abort(403)
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        # Ավտոմատ ողջույն՝ մինչ update-ի մշակումը
        try:
            msg = update.message
            if msg and msg.from_user and not msg.from_user.is_bot and not (msg.text or '').startswith('/start'):
                maybe_greet(msg.chat.id, msg.from_user.first_name or '')
            # 🏅 Ամսվա չեմպիոնի ստուգում (ոչ ավելի հաճախ, քան 6 ժամը մեկ՝ ամեն worker-ի համար)
            if time.time() - _CHAMP_CHECK_TS['ts'] > 21600:
                _CHAMP_CHECK_TS['ts'] = time.time()
                check_month_champion()
        except Exception:
            log.exception("greet hook failed")
        try:
            bot.process_new_updates([update])
        except Exception:
            log.exception("ERROR while processing update")
        return '', 200
    else:
        abort(403)


@app.route('/', methods=['GET'])
def index():
    # Render's health check hits this root route to confirm the
    # Web Service is actually running (and therefore not put to sleep).
    return 'VedaVPN bot is running ✅', 200


# /sub-ի cache՝ GitHub-ից ամեն request-ի ժամանակ չբերելու համար
_SUB_CACHE = {'content': None, 'ts': 0.0}
_SUB_CACHE_TTL = 60  # seconds


@app.route('/sub', methods=['GET'])
def get_sub():
    # Sub ֆայլը բոտը խմբագրում է GitHub-ում, ուստի կարդում ենք հենց GitHub-ից
    # (կարճ cache-ով), այլ ոչ թե deploy-ի պահի հին լոկալ պատճենից։
    now = time.time()
    if _SUB_CACHE['content'] is not None and now - _SUB_CACHE['ts'] < _SUB_CACHE_TTL:
        return _SUB_CACHE['content'], 200, {'Content-Type': 'text/plain; charset=utf-8'}
    try:
        repo, contents = get_sub_file_contents()
        content = contents.decoded_content.decode('utf-8')
        _SUB_CACHE['content'] = content
        _SUB_CACHE['ts'] = now
        return content, 200, {'Content-Type': 'text/plain; charset=utf-8'}
    except Exception:
        log.exception("GitHub sub fetch failed")
        # Fallback 1. հին cache
        if _SUB_CACHE['content'] is not None:
            return _SUB_CACHE['content'], 200, {'Content-Type': 'text/plain; charset=utf-8'}
        # Fallback 2. deploy-ի լոկալ ֆայլ (եթե կա)
        file_path = os.path.join(os.path.dirname(__file__), 'sub')
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read(), 200, {'Content-Type': 'text/plain; charset=utf-8'}
        except FileNotFoundError:
            return "File not found", 404


@app.route('/sub/<token>', methods=['GET'])
def get_sub_personal(token):
    """Անհատական sub հղում. նույն բովանդակությունն է, բայց per-user token-ով։
    Թույլ է տալիս անջատել կոնկրետ օգտատիրոջ (/ban) և տեսնել՝ ով է իրականում օգտվում։"""
    row = db_execute("SELECT user_id, banned FROM users WHERE sub_token = %s", (token,), fetchone=True)
    if not row:
        return "Not found", 404
    if row[1]:
        return "Subscription disabled", 403
    try:
        db_execute(
            "UPDATE users SET sub_fetches = COALESCE(sub_fetches, 0) + 1, last_sub_at = now() WHERE user_id = %s",
            (row[0],), commit=True
        )
    except Exception:
        log.exception("sub fetch counter failed")
    return get_sub()



# ============================================================
# AUTOMATIC SERVER HEALTH CHECK
# Պարբերաբար ստուգում է sub ֆայլի բոլոր սերվերները (TCP connect),
# և N անընդմեջ ձախողումից հետո ինքնաշխատ ջնջում է GitHub sub ֆայլից։
# ============================================================
SERVER_CHECK_INTERVAL = int(os.environ.get('SERVER_CHECK_INTERVAL', 300))   # վայրկյան, default 15 ր.
MAX_FAILS_BEFORE_REMOVE = int(os.environ.get('MAX_FAILS_BEFORE_REMOVE', 1))  # անընդմեջ ձախողումների քանակ
SERVER_CHECK_TIMEOUT = int(os.environ.get('SERVER_CHECK_TIMEOUT', 5))       # TCP connect timeout վրկ

_server_fail_counts = {}  # line -> fail count (in-memory, մաքրվում է restart-ի ժամանակ)


def extract_host_port(line):
    """VLESS/VMESS/SS/Trojan/SSR URI-ից քաշում է (host, port)։"""
    try:
        line = line.strip()
        if not line or line.startswith('#'):
            return None, None
        if line.startswith('vmess://'):
            import base64
            import json
            raw = line[len('vmess://'):].split('#')[0]
            padded = raw + '=' * (-len(raw) % 4)
            data = json.loads(base64.b64decode(padded).decode('utf-8', 'ignore'))
            host = data.get('add')
            port = int(data.get('port')) if data.get('port') else None
            return host, port
        parsed = urlparse(line)
        return parsed.hostname, parsed.port
    except Exception:
        return None, None


def check_server_alive(host, port):
    """Փորձում է TCP միացում հաստատել host:port-ի հետ։"""
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=SERVER_CHECK_TIMEOUT):
            return True
    except Exception:
        return False


def check_all_servers():
    """Ստուգում է sub ֆայլի բոլոր տողերը, և ինքնաշխատ ջնջում է
    այն սերվերները, որոնք MAX_FAILS_BEFORE_REMOVE անընդմեջ ստուգումներում
    անհասանելի են եղել։"""
    try:
        repo, contents = get_sub_file_contents()
        lines = contents.decoded_content.decode('utf-8').split('\n')
    except Exception:
        log.exception("check_all_servers: sub ֆայլը բեռնել չհաջողվեց")
        return

    to_remove = []
    still_seen = set()

    for line in lines:
        if not line.strip() or line.startswith('#'):
            continue
        still_seen.add(line)
        host, port = extract_host_port(line)
        alive = check_server_alive(host, port)
        if alive:
            _server_fail_counts.pop(line, None)
        else:
            _server_fail_counts[line] = _server_fail_counts.get(line, 0) + 1
            if _server_fail_counts[line] >= MAX_FAILS_BEFORE_REMOVE:
                to_remove.append(line)

    # մաքրում ենք հետքերը այն տողերի համար, որոնք արդեն ֆայլում չկան
    for key in list(_server_fail_counts.keys()):
        if key not in still_seen:
            _server_fail_counts.pop(key, None)

    # 🚨 Circuit breaker. եթե ԲՈԼՈՐ սերվերները միաժամանակ անհասանելի են, դա, ամենայն
    # հավանականությամբ, ցանցի/պրովայդերի ընդհանուր խնդիր է (կամ health-check thread-ի
    # իսկ խնդիր), ոչ թե բոլոր սերվերները իրոք մեռած են։ Այս դեպքում ավտոմատ ջնջում/comment
    # չենք անում, որպեսզի sub ֆայլը պատահաբար ամբողջովին չդատարկվի, այլ միայն ադմինին ենք ահազանգում։
    if still_seen and len(to_remove) == len(still_seen):
        try:
            bot.send_message(
                ADMIN_ID,
                "🚨 <b>ԱՀԱԶԱՆԳ:</b> Բոլոր սերվերները միաժամանակ անհասանելի են "
                "(հնարավոր է կապի կամ պրովայդերի խնդիր է)։\nԱվտոմատ ջնջումը կասեցվել է՝ ֆայլը պահպանելու համար:",
                parse_mode="HTML"
            )
        except Exception:
            log.exception("Չհաջողվեց ադմինին ահազանգել բոլոր սերվերների անհասանելիության մասին")
        return

    if not to_remove:
        return

    try:
        # Ամբողջովին ջնջելու փոխարեն՝ comment-ի վերածում ենք (# [AUTO-DISABLED] ...),
        # որպեսզի սերվերի configuration-ը մնա ֆայլում պատմության/ձեռքով վերականգնման
        # համար, և պատահաբար ամբողջովին բան չկորչի։
        new_lines = []
        for l in lines:
            if l in to_remove:
                new_lines.append(f"# [AUTO-DISABLED] {l}")
            else:
                new_lines.append(l)
        new_content_str = '\n'.join(new_lines)
        repo.update_file(
            contents.path,
            f"Auto-disabled {len(to_remove)} dead server(s)",
            new_content_str,
            contents.sha,
        )
        for line in to_remove:
            _server_fail_counts.pop(line, None)
            name, protocol = extract_server_name(line)
            try:
                bot.send_message(
                    ADMIN_ID,
                    f"⚠️ Ավտոմատ կասեցվեց ոչ աշխատող սերվեր՝ {protocol or 'UNKNOWN'} {name or line[:50]}\n(Տողը դարձել է comment ֆայլում)"
                )
            except Exception:
                log.exception("Չհաջողվեց ադմինին ծանուցել կասեցված սերվերի մասին")
    except Exception:
        log.exception("check_all_servers: sub ֆայլը թարմացնել չհաջողվեց")


def _server_check_loop():
    while True:
        try:
            check_all_servers()
        except Exception:
            log.exception("_server_check_loop crashed")
        time.sleep(SERVER_CHECK_INTERVAL)


def start_server_health_check():
    """Առանձին daemon թրեդում գործարկում է սերվերների պարբերական ստուգումը։"""
    threading.Thread(target=_server_check_loop, daemon=True).start()
    log.info(f"Server health-check started (interval={SERVER_CHECK_INTERVAL}s, max_fails={MAX_FAILS_BEFORE_REMOVE})")


def setup_webhook():
    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(url=WEBHOOK_URL, allowed_updates=["message", "callback_query", "chat_member"], secret_token=WEBHOOK_SECRET)
    print(f"✅ Webhook-ը սահմանվեց՝ {WEBHOOK_URL}")


def start_scheduler():
    """Ֆիքսված ժամերով ինքնաշխատ առաջադրանքներ (ամսվա չեմպիոն, ծննդյան օր),
    որպեսզի դրանք տեղի ունենան ճշգրիտ ժամին՝ անկախ trafic-ից (ի տարբերություն
    webhook-ի ներսում եղած throttled ստուգումների, որոնք trafic-ից են կախված)։"""
    scheduler = BackgroundScheduler()
    scheduler.add_job(check_month_champion, 'cron', day=1, hour=0, minute=5)
    scheduler.add_job(daily_birthday_check, 'cron', hour=10, minute=0)
    scheduler.start()
    log.info("APScheduler started (champion: 1st of month 00:05, birthdays: daily 10:00, both in server-local/UTC time).")


# Both of these run at module load time (i.e. also when gunicorn
# imports `bot:app`, not only when running `python bot.py`
# directly).
init_db()
setup_webhook()
start_server_health_check()
start_scheduler()
# Եթե նախորդ deploy/restart-ի պահին broadcast կիսատ էր մնացել, DB-ի
# հերթում մնացած տողերը կան դեռ, ուստի այստեղ ինքնաշխատ շարունակում ենք։
threading.Thread(target=_broadcast_worker_db, daemon=True).start()


if __name__ == '__main__':
    # For local testing. On Render, gunicorn runs `app`,
    # this block is not called there.
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
