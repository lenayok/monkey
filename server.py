# flask_server.py
import os
import json
import hmac
import hashlib
import logging
from urllib.parse import parse_qs, unquote
from flask import Flask, request, jsonify
from flask_cors import CORS # Для разрешения запросов от Web App
import requests # Для отправки команды боту

# --- Настройки ---
FLASK_HOST = '127.0.0.1' # Слушать только на локальном хосте
FLASK_PORT = 5000        # Порт для Flask-сервера
BOT_INTERNAL_API_URL = 'http://127.0.0.1:8081/trigger_conversion' # URL, на котором слушает бот
# ВАЖНО: Используй тот же токен, что и в боте, для валидации initData
BOT_TOKEN = "7910000913:AAG4Mz_wo8Le9ZvItZZMkqtoMUA0VjmEowg" # <--- ЗАМЕНИ НА СВОЙ ТОКЕН

# --- Настройка логирования Flask ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Инициализация Flask ---
app = Flask(__name__)
# Разрешаем запросы от любого источника (для локального теста).
# В продакшене лучше указать конкретный origin твоего WebApp.
CORS(app)

# --- Функция валидации initData ---
def validate_init_data(init_data_str: str, bot_token: str) -> dict | None:
    """Проверяет подлинность данных из Telegram Web App."""
    try:
        parsed_data = parse_qs(init_data_str)
        received_hash = parsed_data.get('hash', [None])[0]
        if not received_hash:
            logger.warning("Hash not found in initData")
            return None

        # Собираем строку для проверки хеша
        data_check_list = []
        for key, value in sorted(parsed_data.items()):
            if key != 'hash':
                # Значения могут быть списком, берем первый элемент
                data_check_list.append(f"{key}={unquote(value[0])}")
        data_check_string = "\n".join(data_check_list)

        # Вычисляем хеш
        secret_key = hmac.new("WebAppData".encode(), bot_token.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        # Сравниваем хеши
        if calculated_hash == received_hash:
            logger.info("initData validation successful.")
            user_data = json.loads(parsed_data.get('user', ['{}'])[0])
            return {
                "user_id": user_data.get('id'),
                "query_id": parsed_data.get('query_id', [None])[0],
                "user_info": user_data
            }
        else:
            logger.warning(f"initData validation failed. Received hash: {received_hash}, Calculated hash: {calculated_hash}")
            return None
    except Exception as e:
        logger.error(f"Error validating initData: {e}", exc_info=True)
        return None

# --- API Эндпоинт ---
@app.route('/api/convert', methods=['POST'])
def handle_convert_request():
    logger.info("Received request on /api/convert")
    if not request.is_json:
        logger.warning("Request is not JSON")
        return jsonify({"status": "error", "message": "Request must be JSON"}), 400

    data = request.get_json()
    logger.info(f"Request JSON data: {data}")

    # Извлекаем данные
    init_data_str = data.get('initData')
    file_name = data.get('file')
    shop = data.get('shop')
    format_type = data.get('format')

    if not all([init_data_str, file_name, shop, format_type]):
        logger.warning("Missing required fields in request")
        return jsonify({"status": "error", "message": "Missing required fields (initData, file, shop, format)"}), 400

    # Валидируем initData
    validation_result = validate_init_data(init_data_str, BOT_TOKEN)
    if not validation_result or not validation_result.get("user_id"):
        logger.warning("initData validation failed or user_id missing")
        return jsonify({"status": "error", "message": "Invalid or missing user authentication"}), 403

    user_id = validation_result["user_id"]
    logger.info(f"Request validated for user_id: {user_id}")

    # Готовим данные для отправки боту
    payload_to_bot = {
        "user_id": user_id,
        "file_name": file_name,
        "shop": shop,
        "format_type": format_type
    }

    # Отправляем команду боту через его внутренний API
    try:
        logger.info(f"Sending trigger request to bot at {BOT_INTERNAL_API_URL}")
        response_from_bot = requests.post(BOT_INTERNAL_API_URL, json=payload_to_bot, timeout=5) # Таймаут 5 секунд
        response_from_bot.raise_for_status() # Вызовет исключение для кодов 4xx/5xx

        bot_response_data = response_from_bot.json()
        if bot_response_data.get("status") == "ok":
            logger.info("Bot accepted the conversion task.")
            # Отвечаем Web App, что все хорошо
            return jsonify({"status": "ok", "message": "Conversion request accepted."}), 200
        else:
            logger.error(f"Bot returned an error: {bot_response_data.get('message')}")
            return jsonify({"status": "error", "message": f"Bot error: {bot_response_data.get('message', 'Unknown error')}"}), 500

    except requests.exceptions.RequestException as e:
        logger.error(f"Could not reach bot at {BOT_INTERNAL_API_URL}: {e}", exc_info=True)
        return jsonify({"status": "error", "message": "Could not communicate with the bot service."}), 503 # Service Unavailable
    except Exception as e:
        logger.error(f"Unexpected error while communicating with bot: {e}", exc_info=True)
        return jsonify({"status": "error", "message": "Internal server error."}), 500

# --- Запуск сервера (ИЗМЕНЕННЫЙ) ---
if __name__ == '__main__':
    # Слушаем на всех доступных сетевых интерфейсах
    FLASK_LISTEN_HOST = '0.0.0.0'
    logger.info(f"Starting Flask server on http://{FLASK_LISTEN_HOST}:{FLASK_PORT}")
    # Используем waitress для более стабильной работы
    try:
        from waitress import serve
        # Указываем host='0.0.0.0' для waitress
        serve(app, host=FLASK_LISTEN_HOST, port=FLASK_PORT)
    except ImportError:
        logger.warning("Waitress not found, using Flask's built-in server.")
        # Указываем host='0.0.0.0' для встроенного сервера Flask
        app.run(host=FLASK_LISTEN_HOST, port=FLASK_PORT, debug=False)
