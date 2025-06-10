from dotenv import load_dotenv
load_dotenv()
import os
import hashlib
import requests
import logging
from flask import Flask, request, jsonify, send_from_directory, redirect

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = Flask(__name__)

# Загрузить настройки из окружения
OK_PUBLIC_KEY = os.getenv('OK_PUBLIC_KEY')
OK_SECRET_KEY = os.getenv('OK_SECRET_KEY')
OK_APP_ID     = os.getenv('OK_APP_ID')
MPETS_API_URL = os.getenv('MPETS_API_URL')
BOT_TOKEN     = os.getenv('BOT_TOKEN')  # Telegram bot token

### Утилиты ###
def make_sig(params: dict) -> str:
    s = ''.join(f"{k}={v}" for k, v in sorted(params.items())) + OK_SECRET_KEY
    return hashlib.md5(s.encode('utf-8')).hexdigest()

# Общий запрос к OK API
def ok_api_request(method: str, params: dict) -> dict:
    base = {'method': method, 'application_key': OK_PUBLIC_KEY, 'format': 'json'}
    base.update(params)
    base['sig'] = make_sig(base)
    logging.info("OK API Request: %s with %s", method, params)
    resp = requests.post('https://api.ok.ru/fb.do', data=base)
    return resp.json()

# Отправка сообщений от сообщества
def send_ok(uid: str, text: str, template: dict = None):
    params = {'access_token': os.getenv(f'TOKEN_{uid}', ''), 'uid': uid, 'message': text}
    if template:
        params['template'] = template
    return ok_api_request('mediatopic.post', params)

### Интеграция mpets.mobi ###
def mpets_start_game(uid: str) -> str:
    return requests.get(f'{MPETS_API_URL}/start_game', params={'user': uid}).json().get('result', 'окей!')

def mpets_feed_pet(uid: str) -> str:
    return requests.get(f'{MPETS_API_URL}/feed', params={'user': uid}).json().get('result', 'наелся!')

def mpets_show_exhibition(uid: str) -> str:
    return requests.get(f'{MPETS_API_URL}/exhibition', params={'user': uid}).json().get('result', 'гял!')

def mpets_walk(uid: str) -> str:
    return requests.get(f'{MPETS_API_URL}/walk', params={'user': uid}).json().get('result', 'гуляем!')

def mpets_meadow(uid: str) -> str:
    return requests.get(f'{MPETS_API_URL}/meadow', params={'user': uid}).json().get('result', 'поляна тут!')

### Маппинг команд ###
COMMAND_HANDLERS = {
    'играть':    lambda uid: f"🚀 Поехали! {mpets_start_game(uid)}",
    'кормить':   lambda uid: f"🍖 Твой питомец сыт: {mpets_feed_pet(uid)}",
    'выставка':  lambda uid: f"🖼️ Выставка: {mpets_show_exhibition(uid)}",
    'прогулка':  lambda uid: f"🚶 Прогулка: {mpets_walk(uid)}",
    'поляна':    lambda uid: f"🌿 Поляна: {mpets_meadow(uid)}",
}

### Меню для OK ###
MAIN_MENU_TEMPLATE = {
    "type": "buttons",
    "buttons": [
        {"title": "Играть",  "payload": "играть"},
        {"title": "Кормить", "payload": "кормить"},
        {"title": "Выставка","payload": "выставка"},
        {"title": "Прогулка","payload": "прогулка"},
        {"title": "Поляна",  "payload": "поляна"},
    ]
}

def send_main_menu(uid: str):
    return send_ok(uid, "Чё будем мутить?", MAIN_MENU_TEMPLATE)

### Роуты для OK ###
@app.route('/')
def index():
    logging.info("Root path accessed")
    return send_from_directory('templates', 'index.html')

@app.route('/oauth/callback')
def oauth_callback():
    # Обработка OAuth callback от ОК
    code = request.args.get('code')
    if not code:
        logging.error("OAuth callback without code")
        return "Недостаточно параметров для авторизации", 400
    logging.info("OAuth callback code: %s", code)
    try:
        resp = requests.get('https://api.ok.ru/oauth/token.do', params={
            'client_id':     OK_APP_ID,
            'client_secret': OK_SECRET_KEY,
            'redirect_uri':  'https://mpetsok.onrender.com/oauth/callback',
            'grant_type':    'authorization_code',
            'code':          code
        })
        resp.raise_for_status()
        data = resp.json()
        logging.info("OAuth token response: %s", data)
        # TODO: сохранить data['access_token'], data['refresh_token'], data['user_id'] в БД
        # Закрываем окно для пользователя после успешной авторизации
        return ('<html><body>'
                '<script>window.close();</script>'
                'Авторизация успешна! Вы можете вернуться в Telegram.'
                '</body></html>')
    except Exception:
        logging.exception("Ошибка при обмене кода на токен")
        return "Ошибка сервера при авторизации", 500

@app.route('/webhook', methods=['POST'])
def ok_webhook():
    data = request.json.get('notification', {})
    logging.info("OK webhook data: %s", data)
    ntype = data.get('type')

    if ntype == 'message':
        msg = data['message']
        uid = msg['sender']['uid']
        text = msg.get('text', '').strip().lower()
        handler = COMMAND_HANDLERS.get(text)
        if handler:
            return send_ok(uid, handler(uid))
        return send_main_menu(uid)

    if ntype == 'click':
        uid = data['uid']
        payload = data.get('payload', '').strip().lower()
        handler = COMMAND_HANDLERS.get(payload)
        if handler:
            return send_ok(uid, handler(uid))
        return send_ok(uid, 'Не понимаю, бро… Вот кнопки, жми:', MAIN_MENU_TEMPLATE)

    return jsonify({'error': 'unsupported event'}), 400

### Telegram Webhook ###

def send_telegram(chat_id, text, reply_markup=None):
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
    payload = {'chat_id': chat_id, 'text': text}
    if reply_markup:
        payload['reply_markup'] = reply_markup
    logging.info("Sending Telegram message: %s to %s", text, chat_id)
    requests.post(url, json=payload)

@app.route('/telegram_webhook', methods=['POST'])
def telegram_webhook():
    update = request.json
    logging.info("Telegram update received: %s", update)
    if 'callback_query' in update:
        query = update['callback_query']
        chat_id = query['message']['chat']['id']
        data = query.get('data')
        requests.post(
            f'https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery',
            json={'callback_query_id': query.get('id')}
        )
        if data == 'add_account':
            # OAuth authorization via Telegram Web App
            oauth_url = (
                f"https://connect.ok.ru/oauth/authorize?client_id={OK_APP_ID}"
                f"&redirect_uri=https://mpetsok.onrender.com/oauth/callback"
                f"&scope=VALUABLE_ACCESS,LONG_ACCESS_TOKEN"
                f"&response_type=code"
            )
            # Используем web_app, чтобы открыть OAuth попап внутри Telegram
            web_app_button = {'text': 'Авторизоваться в ОК', 'web_app': {'url': oauth_url}}
            keyboard = [[web_app_button]]
            send_telegram(chat_id, 'Нажми, чтобы авторизоваться:', {'inline_keyboard': keyboard})
        elif data == 'manage_accounts':
            send_telegram(chat_id, 'Управление аккаунтами пока не доступно.', None)
        return jsonify({'ok': True})

    msg = update.get('message') or update.get('edited_message')
    if not msg:
        return jsonify({'ok': True})

    chat_id = msg['chat']['id']
    text = msg.get('text', '').strip().lower()

    if text == '/start':
        keyboard = [
            [{'text': 'Добавить аккаунт', 'callback_data': 'add_account'}],
            [{'text': 'Управление аккаунтами', 'callback_data': 'manage_accounts'}]
        ]
        send_telegram(chat_id, 'Привет! Что хочешь сделать?', {'inline_keyboard': keyboard})
    else:
        handler = COMMAND_HANDLERS.get(text)
        if handler:
            send_telegram(chat_id, handler(str(chat_id)))
        else:
            send_telegram(chat_id, 'Не понимаю, бро…', None)

    return jsonify({'ok': True})

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    logging.info("Starting app on port %s", port)
    app.run(host='0.0.0.0', port=port)