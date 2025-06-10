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

# Загружаем переменные окружения из .env
load_dotenv()

app = Flask(__name__)

# Конфигурация из окружения
OK_PUBLIC_KEY = os.getenv('OK_PUBLIC_KEY')
OK_SECRET_KEY = os.getenv('OK_SECRET_KEY')
OK_APP_ID     = os.getenv('OK_APP_ID')
MPETS_API_URL = os.getenv('MPETS_API_URL')  # https://odkl.mpets.mobi
BOT_TOKEN     = os.getenv('BOT_TOKEN')

# Хранилище OAuth state и задач авто-режима
STATE_MAP = {}      # state -> chat_id
TASKS = {}          # chat_id -> asyncio.Future

# Создаём отдельный asyncio loop для фоновых задач
BG_LOOP = asyncio.new_event_loop()
threading.Thread(target=BG_LOOP.run_forever, daemon=True).start()

### Утилиты для работы с API Одноклассников ###
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

### Endpoints для OAuth ###
@app.route('/oauth/callback')
def oauth_callback():
    code = request.args.get('code')
    state = request.args.get('state')
    if not code or not state:
        logging.error("Missing code or state in callback: %s %s", code, state)
        return "Invalid callback parameters", 400

    # Найти Telegram chat_id по state
    chat_id = STATE_MAP.pop(state, None)
    logging.info("OAuth callback received: code=%s, state=%s", code, state)
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
        logging.info("OAuth token response: %s", data)
        # TODO: сохранить access_token и user_id, ассоциировать с chat_id
        if chat_id:
            send_telegram(chat_id, 'Авторизация в ОК успешна! Аккаунт добавлен.')
        # Закрываем окно браузера
        return '<html><body><script>window.close();</script></body></html>'
    except Exception:
        logging.exception("Error exchanging code for token")
        return "Server error during OAuth", 500

### Async авто-действия (Одноклассники) ###
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
    # Разворачиваем cookies
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
                logging.info(f"Auto actions for {session_name} cancelled")
                break
            # Первые 4 действия по 6 раз
            for url in urls[:4]:
                for _ in range(6):
                    await visit_url(web_session, url, session_name)
                    await asyncio.sleep(1)
            # Остальные по 1 разу
            for url in urls[4:]:
                await visit_url(web_session, url, session_name)
                await asyncio.sleep(1)
            # Путешествия от id=10 до 1
            for i in range(10, 0, -1):
                await visit_url(web_session, f"https://odkl.mpets.mobi/go_travel?id={i}", session_name)
                await asyncio.sleep(1)
            await asyncio.sleep(60)

async def visit_url(web_session, url, session_name):
    try:
        async with web_session.get(url) as resp:
            if resp.status == 200:
                logging.info(f"[{session_name}] Success {url}")
            else:
                logging.error(f"[{session_name}] Error {resp.status} {url}")
    except Exception as e:
        logging.error(f"[{session_name}] Exception at {url}: {e}")

### Интеграция с Telegram ###
def send_telegram(chat_id, text, reply_markup=None):
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
    payload = {'chat_id': chat_id, 'text': text}
    if reply_markup:
        payload['reply_markup'] = reply_markup
    requests.post(url, json=payload)

@app.route('/telegram_webhook', methods=['POST'])
def telegram_webhook():
    update = request.json
    # Callback query
    if 'callback_query' in update:
        query = update['callback_query']
        chat_id = query['message']['chat']['id']
        data = query.get('data')
        # Подтверждаем кнопку
        requests.post(
            f'https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery',
            json={'callback_query_id': query['id']}
        )
        if data == 'on':
            if chat_id in TASKS:
                send_telegram(chat_id, 'Auto already running')
            else:
                # Получить cookies пользователя (из БД или сессии)
                cookies = []  # TODO: заменить на реальные cookies
                task = asyncio.run_coroutine_threadsafe(auto_actions(cookies, chat_id), BG_LOOP)
                TASKS[chat_id] = task
                send_telegram(chat_id, 'Auto actions enabled')
        elif data == 'off':
            task = TASKS.pop(chat_id, None)
            if task:
                task.cancel()
                send_telegram(chat_id, 'Auto actions disabled')
            else:
                send_telegram(chat_id, 'Auto not active')
        return jsonify(ok=True)

    # Message handling
    msg = update.get('message')
    if not msg:
        return jsonify(ok=True)
    chat_id = msg['chat']['id']
    text = msg.get('text', '').strip().lower()
    if text == '/start':
        keyboard = [
            [{'text': 'ON', 'callback_data': 'on'}],
            [{'text': 'OFF', 'callback_data': 'off'}]
        ]
        send_telegram(chat_id, 'Manage auto actions:', {'inline_keyboard': keyboard})
    return jsonify(ok=True)

@app.route('/')
def index():
    return send_from_directory('templates', 'index.html')

@app.route('/webhook', methods=['POST'])
def ok_webhook():
    # Webhook from OK (no-op for now)
    return jsonify({})

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    logging.info(f"Starting app on port {port}")
    app.run(host='0.0.0.0', port=port)