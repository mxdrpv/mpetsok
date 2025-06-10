import os
import hashlib
import logging
import uuid
import asyncio
import threading
from dotenv import load_dotenv
from aiohttp import ClientSession, CookieJar
from flask import Flask, request, jsonify, send_from_directory, redirect
import requests

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Загружаем переменные окружения
load_dotenv()

app = Flask(__name__)

# Конфигурация из окружения
OK_PUBLIC_KEY = os.getenv('OK_PUBLIC_KEY')
OK_SECRET_KEY = os.getenv('OK_SECRET_KEY')
OK_APP_ID     = os.getenv('OK_APP_ID')
MPETS_API_URL = os.getenv('MPETS_API_URL')  # https://odkl.mpets.mobi
BOT_TOKEN     = os.getenv('BOT_TOKEN')

# Хранилище OAuth state, авторизованных пользователей и задач авто-режима
STATE_MAP = {}      # state -> chat_id
AUTHORIZED = set()  # chat_id после OAuth
TASKS = {}          # chat_id -> asyncio.Future

# Запускаем отдельный asyncio loop для фоновых задач
BG_LOOP = asyncio.new_event_loop()
threading.Thread(target=BG_LOOP.run_forever, daemon=True).start()

### Утилиты для OK API ###
def make_sig(params: dict) -> str:
    s = ''.join(f"{k}={v}" for k, v in sorted(params.items())) + OK_SECRET_KEY
    return hashlib.md5(s.encode('utf-8')).hexdigest()

def ok_api_request(method: str, params: dict) -> dict:
    base = {'method': method, 'application_key': OK_PUBLIC_KEY, 'format': 'json'}
    base.update(params)
    base['sig'] = make_sig(base)
    logging.info("OK API Request: %s %s", method, params)
    resp = requests.post('https://api.ok.ru/fb.do', data=base)
    return resp.json()

def send_ok(uid: str, text: str, template: dict = None):
    params = {'access_token': os.getenv(f'TOKEN_{uid}', ''), 'uid': uid, 'message': text}
    if template:
        params['template'] = template
    return ok_api_request('mediatopic.post', params)

### OAuth Callback ###
@app.route('/oauth/callback')
def oauth_callback():
    code = request.args.get('code')
    state = request.args.get('state')
    if not code or not state:
        logging.error("Invalid OAuth callback, missing code or state")
        return "Invalid callback parameters", 400
    chat_id = STATE_MAP.pop(state, None)
    logging.info("OAuth callback: code=%s state=%s chat_id=%s", code, state, chat_id)
    try:
        resp = requests.post('https://api.ok.ru/oauth/token.do', data={
            'client_id': OK_APP_ID,
            'client_secret': OK_SECRET_KEY,
            'redirect_uri': 'https://mpetsok.onrender.com/oauth/callback',
            'grant_type': 'authorization_code',
            'code': code
        })
        resp.raise_for_status()
        data = resp.json()
        access_token = data.get('access_token')
        user_id = data.get('user_id') or data.get('session_key')
        logging.info("OAuth token received: %s", data)
        if chat_id:
            # помечаем пользователя как авторизованного
            AUTHORIZED.add(chat_id)
            # TODO: сохранить access_token и user_id в БД, связать с chat_id
            send_telegram(chat_id, 'Авторизация ОК успешна!')
        return '<html><body><script>window.close();</script></body></html>'
    except Exception:
        logging.exception("Error during OAuth token exchange")
        return "Server error during OAuth", 500

### Async Auto-Actions ###
async def auto_actions(session_cookies, session_name):
    urls = [
        "https://odkl.mpets.mobi/?action=food",
        "https://odkl.mpets.mobi/?action=play",
        "https://odkl.mpets.mobi/show",
        "https://odkl.mpets.mobi/glade_dig",
        "https://odkl.mpets.mobi/show_coin_get",
        "https://odkl.mpets.mobi/task_reward?id=46",
        "https://odkl.mpets.mobi/task_reward?id=49",
        "https://odkl.mpets.mobi/task_reward?id=52"
    ]
    # собираем cookies
    if isinstance(session_cookies, list):
        cookies = {c['name']: c['value'] for c in session_cookies}
    else:
        cookies = session_cookies.get('cookies', session_cookies)
    jar = CookieJar()
    jar.update_cookies(cookies)

    async with ClientSession(cookie_jar=jar) as web_session:
        while True:
            task = asyncio.current_task()
            if task.cancelled():
                logging.info(f"Auto-actions for {session_name} cancelled")
                break
            # первые 4 действия по 6 раз
            for url in urls[:4]:
                for _ in range(6):
                    await visit_url(web_session, url, session_name)
                    await asyncio.sleep(1)
            # остальные действия по разу
            for url in urls[4:]:
                await visit_url(web_session, url, session_name)
                await asyncio.sleep(1)
            # путешествия id от 10 до 1
            for i in range(10, 0, -1):
                travel_url = f"https://odkl.mpets.mobi/go_travel?id={i}"
                await visit_url(web_session, travel_url, session_name)
                await asyncio.sleep(1)
            # пауза между циклами
            await asyncio.sleep(60)

async def visit_url(web_session, url, session_name):
    try:
        async with web_session.get(url) as resp:
            if resp.status == 200:
                logging.info(f"[{session_name}] OK {url}")
            else:
                logging.error(f"[{session_name}] ERR {resp.status} {url}")
    except Exception as e:
        logging.error(f"[{session_name}] Exception {e} at {url}")

### Telegram Integration ###
def send_telegram(chat_id, text, reply_markup=None):
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
    payload = {'chat_id': chat_id, 'text': text}
    if reply_markup:
        payload['reply_markup'] = reply_markup
    requests.post(url, json=payload)

@app.route('/telegram_webhook', methods=['POST'])
def telegram_webhook():
    update = request.json
    # inline callback processing
    if 'callback_query' in update:
        q = update['callback_query']
        cid = q['message']['chat']['id']
        data = q.get('data')
        # acknowledge
        requests.post(f'https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery', json={'callback_query_id': q['id']})
        if data == 'add_account':
            # Открываем мини-приложение OK в Telegram Web App
            mini_app_url = f"https://ok.ru/game/{OK_APP_ID}"
            web_app_button = {'text': 'Запустить OK мини-приложение', 'web_app': {'url': mini_app_url}}
            send_telegram(cid, 'Нажми для открытия мини-приложения Одноклассников:', {'inline_keyboard': [[web_app_button]]})
        elif data == 'on' and cid in AUTHORIZED:
            cookies = []  # TODO: load real cookies from DB
            task = asyncio.run_coroutine_threadsafe(auto_actions(cookies, cid), BG_LOOP)
            TASKS[cid] = task
            send_telegram(cid, 'Auto actions enabled')
        elif data == 'off' and cid in AUTHORIZED:
            task = TASKS.pop(cid, None)
            if task:
                task.cancel()
                send_telegram(cid, 'Auto actions disabled')
        return jsonify(ok=True)
    # message handling
    msg = update.get('message')
    if msg and 'text' in msg:
        cid = msg['chat']['id']
        txt = msg['text'].strip().lower()
        if txt == '/start':
            if cid not in AUTHORIZED:
                kb = [[{'text': 'Добавить аккаунт', 'callback_data': 'add_account'}]]
                send_telegram(cid, 'Сначала авторизуйся в ОК', {'inline_keyboard': kb})
            else:
                kb = [[{'text': 'ON', 'callback_data': 'on'}], [{'text': 'OFF', 'callback_data': 'off'}]]
                send_telegram(cid, 'Управление авто-режимом', {'inline_keyboard': kb})
    return jsonify(ok=True)

@app.route('/webhook', methods=['POST'])
def ok_webhook():
    # no-op for OK webhooks
    return jsonify({})

@app.route('/')
def index():
    return send_from_directory('templates', 'index.html')

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    logging.info(f"Starting on port {port}")
    app.run(host='0.0.0.0', port=port)