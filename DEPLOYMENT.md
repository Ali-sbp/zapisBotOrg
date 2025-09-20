# Deployment Guide / Руководство по Развертыванию

[English](#english) | [Русский](#русский)

---

## English

### Quick Start

#### 1. Install Dependencies

```bash
pip install python-telegram-bot==22.4 python-dotenv==1.1.1 pytz==2025.2 apscheduler==3.11.0
```

#### 2. Configure the Bot

Create `.env` file with your bot token:
```bash
# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather
DEBUG=True
```

#### 3. Configure Courses

Edit `config.json` with your courses and schedules:
```json
{
  "courses": {
    "course1": {
      "name": "Course Name",
      "schedule": {"day": 0, "time": "18:00"}
    }
  },
  "admins": [your_telegram_user_id]
}
```

#### 4. Run the Bot

```bash
# Using Python virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py

# Or directly
python main.py
```

### Adding to Group Chat

1. Add your bot to the university group chat
2. Ensure bot has permission to send messages and use inline keyboards
3. Students can use `/start` to see available commands
4. Commands work in both private messages and group chats

### Testing Commands

#### User Commands
- `/start` - Welcome message with all commands
- `/list` - View all courses and their schedules
- `/register` - Register for open courses
- `/status` - Check queue status for all courses
- `/myregistrations` - View your registrations

#### Admin Commands (for testing)
- `/admin_open` - Manually open registration for courses
- `/admin_close` - Close registration for courses
- `/admin_clear` - Clear specific course queues
- `/admin_clear_all` - Clear all queues
- `/admin_status` - Detailed system status
- `/admin_add_course` - Add new courses dynamically
- `/admin_remove_course` - Remove courses

### System Features

#### Per-Course Management
- Each course has individual registration schedule
- Independent queue management
- Selective opening/closing of registration
- Course-specific clearing operations

#### Network Resilience
- 30-second connection timeouts
- Automatic retry for network failures
- Graceful error handling for Telegram API issues
- Continues operation despite temporary network problems

#### Data Persistence
- JSON-based storage in `queue_data.json`
- Automatic backups on data changes
- Survives bot restarts
- Course configurations in `config.json`

### Production Deployment

#### Recommended Infrastructure
```bash
# On Ubuntu/Debian server
sudo apt update
sudo apt install python3 python3-pip python3-venv git

# Clone repository
git clone <your-repo-url>
cd tgbot3

# Setup virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure environment
cp .env.example .env  # Edit with your bot token
nano config.json     # Configure courses
```

#### Process Management with systemd

Create `/etc/systemd/system/tgbot.service`:
```ini
[Unit]
Description=University Course Registration Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/path/to/tgbot3
Environment=PATH=/path/to/tgbot3/.venv/bin
ExecStart=/path/to/tgbot3/.venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable tgbot
sudo systemctl start tgbot
sudo systemctl status tgbot
```

#### Alternative: Docker Deployment

Create `Dockerfile`:
```dockerfile
FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
CMD ["python", "main.py"]
```

Build and run:
```bash
docker build -t tgbot .
docker run -d --name course-bot --restart unless-stopped tgbot
```

### Monitoring and Maintenance

#### Logging
- Bot logs important events to console
- Set `DEBUG=True` for detailed logging
- Consider redirecting logs to file in production:
  ```bash
  python main.py >> bot.log 2>&1
  ```

#### Health Monitoring
- Check bot responsiveness with `/start` command
- Monitor `queue_data.json` for data persistence
- Watch system resources (CPU, memory, disk space)

#### Backup Strategy
```bash
# Backup configuration and data
cp config.json config.json.backup
cp queue_data.json queue_data.json.backup
cp .env .env.backup
```

### Security Considerations

- Keep bot token secure (never commit to version control)
- Implement proper admin verification
- Consider rate limiting for heavy usage
- For production: migrate to PostgreSQL/MySQL
- Use HTTPS webhooks instead of polling for better performance

### Troubleshooting

#### Common Issues

1. **Bot not responding**
   - Verify token in `.env`
   - Check network connectivity
   - Ensure bot is not blocked by Telegram

2. **Commands not working**
   - Verify bot has message permissions in groups
   - Check if commands are registered properly
   - Test in private chat first

3. **Schedule not working**
   - Check timezone settings
   - Verify schedule format in `config.json`
   - Ensure scheduler is running (check logs)

4. **Data not persisting**
   - Check file permissions for `queue_data.json`
   - Verify disk space availability
   - Check for JSON format errors

#### Debug Mode

Enable comprehensive logging:
```bash
DEBUG=True python main.py
```

This shows:
- HTTP requests to Telegram API
- Schedule processing
- Queue operations
- Error details

---

## Русский

### Быстрый Старт

#### 1. Установка Зависимостей

```bash
pip install python-telegram-bot==22.4 python-dotenv==1.1.1 pytz==2025.2 apscheduler==3.11.0
```

#### 2. Настройка Бота

Создайте файл `.env` с токеном бота:
```bash
# Конфигурация Telegram Бота
TELEGRAM_BOT_TOKEN=ваш_токен_от_botfather
DEBUG=True
```

#### 3. Настройка Курсов

Отредактируйте `config.json` с вашими курсами и расписаниями:
```json
{
  "courses": {
    "course1": {
      "name": "Название Курса",
      "schedule": {"day": 0, "time": "18:00"}
    }
  },
  "admins": [ваш_telegram_user_id]
}
```

#### 4. Запуск Бота

```bash
# Использование виртуального окружения (рекомендуется)
python -m venv .venv
source .venv/bin/activate  # В Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py

# Или напрямую
python main.py
```

### Добавление в Групповой Чат

1. Добавьте бота в групповой чат университета
2. Убедитесь, что у бота есть права отправлять сообщения и использовать клавиатуры
3. Студенты могут использовать `/start` для просмотра доступных команд
4. Команды работают как в личных сообщениях, так и в групповых чатах

### Команды для Тестирования

#### Команды Пользователя
- `/start` - Приветственное сообщение со всеми командами
- `/list` - Просмотр всех курсов и их расписаний
- `/register` - Регистрация на открытые курсы
- `/status` - Проверка статуса очередей для всех курсов
- `/myregistrations` - Просмотр ваших регистраций

#### Команды Администратора (для тестирования)
- `/admin_open` - Ручное открытие регистрации для курсов
- `/admin_close` - Закрытие регистрации для курсов
- `/admin_clear` - Очистка очередей конкретных курсов
- `/admin_clear_all` - Очистка всех очередей
- `/admin_status` - Подробный статус системы
- `/admin_add_course` - Динамическое добавление новых курсов
- `/admin_remove_course` - Удаление курсов

### Возможности Системы

#### Управление по Курсам
- У каждого курса индивидуальное расписание регистрации
- Независимое управление очередями
- Селективное открытие/закрытие регистрации
- Операции очистки для конкретных курсов

#### Сетевая Устойчивость
- 30-секундные таймауты соединения
- Автоматические повторы при сетевых сбоях
- Грациозная обработка ошибок Telegram API
- Продолжение работы несмотря на временные сетевые проблемы

#### Сохранение Данных
- Хранение на основе JSON в `queue_data.json`
- Автоматические резервные копии при изменении данных
- Переживает перезапуски бота
- Конфигурации курсов в `config.json`

### Продуктивное Развертывание

#### Рекомендуемая Инфраструктура
```bash
# На сервере Ubuntu/Debian
sudo apt update
sudo apt install python3 python3-pip python3-venv git

# Клонирование репозитория
git clone <url-вашего-репозитория>
cd tgbot3

# Настройка виртуального окружения
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Настройка окружения
cp .env.example .env  # Отредактируйте с вашим токеном бота
nano config.json     # Настройте курсы
```

#### Управление Процессами с systemd

Создайте `/etc/systemd/system/tgbot.service`:
```ini
[Unit]
Description=University Course Registration Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/путь/к/tgbot3
Environment=PATH=/путь/к/tgbot3/.venv/bin
ExecStart=/путь/к/tgbot3/.venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Включение и запуск:
```bash
sudo systemctl daemon-reload
sudo systemctl enable tgbot
sudo systemctl start tgbot
sudo systemctl status tgbot
```

### Мониторинг и Обслуживание

#### Логирование
- Бот логирует важные события в консоль
- Установите `DEBUG=True` для подробного логирования
- В продакшене направляйте логи в файл:
  ```bash
  python main.py >> bot.log 2>&1
  ```

#### Мониторинг Работоспособности
- Проверяйте отзывчивость бота командой `/start`
- Отслеживайте `queue_data.json` для сохранения данных
- Следите за системными ресурсами (CPU, память, место на диске)

### Устранение Неполадок

#### Частые Проблемы

1. **Бот не отвечает**
   - Проверьте токен в `.env`
   - Проверьте сетевое соединение
   - Убедитесь, что бот не заблокирован Telegram

2. **Команды не работают**
   - Проверьте права бота на отправку сообщений в группах
   - Проверьте правильность регистрации команд
   - Сначала протестируйте в личном чате

3. **Расписание не работает**
   - Проверьте настройки часового пояса
   - Проверьте формат расписания в `config.json`
   - Убедитесь, что планировщик запущен (проверьте логи)

#### Режим Отладки

Включите полное логирование:
```bash
DEBUG=True python main.py
```

Показывает:
- HTTP запросы к Telegram API
- Обработку расписания
- Операции с очередями
- Подробности ошибок