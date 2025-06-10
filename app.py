from dotenv import load_dotenv
load_dotenv()
import os
import hashlib
import requests
from flask import Flask, request, jsonify, send_from_directory, redirect

app = Flask(__name__)

# Загружаем настройки
OK_PUBLIC_KEY = os.getenv('OK_PUBLIC_KEY')
OK_SECRET_KEY = os.getenv('OK_SECRET_KEY')
OK_APP_ID     = os.getenv('OK_APP_ID')
MPETS_API_URL = os.getenv('MPETS_API_URL')

# Утилиты для подписи OK API
def make_sig(params: dict) -> str:
    s = ''.join(f"{k}={v}" for k, v in sorted(params.items()))
    s += OK_SECRET_KEY
    return hashlib.md5(s.encode('utf-8')).hexdigest()

# Общий запрос к API ОК
def ok_api_request(method: str, params: dict) -> dict:
    base = {'method': method, 'application_key': OK_PUBLIC_KEY, 'format': 'json'}
    base.update(params)
    base['sig'] = make_sig(base)
    resp = requests.post('https://api.ok.ru/fb.do', data=base)
    return resp.json()

# Отправка сообщения от сообщества
def send_message(uid: str, text: str, template: dict = None):
    params = {'access_token': os.getenv(f'TOKEN_{uid}', ''), 'uid': uid, 'message': text}
    if template:
        params['template'] = template
    return ok_api_request('mediatopic.post', params)

# Заглушки интеграции mpets.mobi
def mpets_start_game(uid: str) -> str:
    r = requests.get(f'{MPETS_API_URL}/start_game', params={'user': uid})
    return r.json().get('result', 'окей!')

def mpets_feed_pet(uid: str) -> str:
    r = requests.get(f'{MPETS_API_URL}/feed', params={'user': uid})
    return r.json().get('result', 'наелся!')

def mpets_show_exhibition(uid: str) -> str:
    r = requests.get(f'{MPETS_API_URL}/exhibition', params={'user': uid})
    return r.json().get('result', 'гял!')

def mpets_walk(uid: str) -> str:
    r = requests.get(f'{MPETS_API_URL}/walk', params={'user': uid})
    return r.json().get('result', 'гуляем!')

def mpets_meadow(uid: str) -> str:
    r = requests.get(f'{MPETS_API_URL}/meadow', params={'user': uid})
    return r.json().get('result', 'поляна тут!')

# Мапа команд
COMMAND_HANDLERS = {
    'играть': lambda uid: f"🚀 Поехали! {mpets_start_game(uid)}",
    'кормить': lambda uid: f"🍖 Твой питомец сыт: {mpets_feed_pet(uid)}",
    'выставка': lambda uid: f"🖼️ Выставка: {mpets_show_exhibition(uid)}",
    'прогулка': lambda uid: f"🚶 Прогулка: {mpets_walk(uid)}",
    'поляна': lambda uid: f"🌿 Поляна: {mpets_meadow(uid)}",
}

# Главное меню
MAIN_MENU_TEMPLATE = {
    "type": "buttons",
    "buttons": [
        {"title": "Играть",   "payload": "играть"},
        {"title": "Кормить",  "payload": "кормить"},
        {"title": "Выставка", "payload": "выставка"},
        {"title": "Прогулка", "payload": "прогулка"},
        {"title": "Поляна",   "payload": "поляна"},
    ]
}

def send_main_menu(uid: str):
    return send_message(uid, "Чё будем мутить?", MAIN_MENU_TEMPLATE)

# Основные маршруты
@app.route('/')
def index():
    return send_from_directory('templates', 'index.html')

@app.route('/oauth/callback')
def oauth_callback():
    code = request.args.get('code')
    resp = requests.get('https://api.ok.ru/oauth/token.do', params={
        'client_id':     OK_APP_ID,
        'client_secret': OK_SECRET_KEY,
        'redirect_uri':  'https://mpetsok.onrender.com/oauth/callback',
        'grant_type':    'authorization_code',
        'code':          code
    })
    data = resp.json()
    # Сохрани data['access_token'], data['refresh_token'], data['user_id'] в БД
    return 'Авторизация успешна! Можешь закрыть это окно.'

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json.get('notification', {})
    ntype = data.get('type')
    uid = None

    if ntype == 'message':
        msg = data.get('message', {})
        uid = msg.get('sender', {}).get('uid')
        text = msg.get('text', '').strip().lower()
        handler = COMMAND_HANDLERS.get(text)
        if handler and uid:
            reply = handler(uid)
            return send_message(uid, reply)
        else:
            return send_main_menu(uid)

    if ntype == 'click':
        uid = data.get('uid')
        payload = data.get('payload', '').strip().lower()
        handler = COMMAND_HANDLERS.get(payload)
        if handler and uid:
            reply = handler(uid)
            return send_message(uid, reply)
        else:
            return send_message(uid, 'Не понимаю, бро… Вот кнопки, жми:', MAIN_MENU_TEMPLATE)

    return jsonify({'error': 'unsupported event'}), 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))