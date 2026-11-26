# ruff: noqa: RUF001
"""Interface translations. Article and model output keep their original language."""

from collections.abc import Callable

RUSSIAN = {
    "Language": "Язык интерфейса",
    "Skip to content": "К содержимому",
    "Newsroom home": "Главная страница Newsroom",
    "Workspace": "Редакторская панель",
    "Queue": "Очередь",
    "Sources": "Источники",
    "Sign out": "Выйти",
    "Sign in": "Войти",
    "Local demo": "Локальное демо",
    "Editor sign-in": "Вход в редакторскую панель",
    "Admin password": "Пароль администратора",
    "Editorial queue": "Очередь на проверку",
    "Review source material, refine drafts and approve posts for Telegram.": (
        "Сверяйте черновики с источниками, редактируйте и одобряйте посты для Telegram."
    ),
    "Candidates": "Кандидаты",
    "Queue overview": "Сводка очереди",
    "Ready to score": "Готовы к оценке",
    "Drafted candidates": "Есть черновики",
    "Passed over": "Пропущены",
    "Status": "Статус",
    "Apply": "Применить",
    "Loading…": "Загрузка…",
    "All candidates": "Все кандидаты",
    "Needs review": "На проверке",
    "Drafted": "Есть черновик",
    "No candidates match this filter.": "По этому фильтру ничего не найдено.",
    "Collect articles from": "Соберите статьи в разделе",
    "or choose another status.": "или выберите другой статус.",
    "Queue pages": "Страницы очереди",
    "Previous": "Назад",
    "Page": "Страница",
    "Next": "Далее",
    "Verify RSS feeds, check recent collection and manage what enters the queue.": (
        "Проверяйте RSS-ленты, смотрите результаты сбора и управляйте источниками очереди."
    ),
    "Collection result": "Результаты сбора",
    "Enable a verified feed before collecting articles.": (
        "Включите проверенную ленту, чтобы собрать статьи."
    ),
    "Feed directory": "Список лент",
    "Collect now": "Собрать статьи",
    "Last success": "Последний успешный сбор",
    "not collected yet": "сборов ещё не было",
    "Enabled": "Включена",
    "Paused": "На паузе",
    "Pause": "Приостановить",
    "Enable": "Включить",
    "No approved sources.": "Пока нет одобренных источников.",
    "Verify a feed": "Проверить ленту",
    "RSS or Atom URL": "Адрес RSS или Atom",
    "Additional approved hostnames": "Дополнительные разрешённые домены",
    "Comma-separated. Only add domains where this feed's articles are hosted.": (
        "Укажите через запятую только домены, на которых опубликованы статьи этой ленты."
    ),
    "Verify and enable": "Проверить и включить",
    "Request error": "Ошибка запроса",
    "Request could not be completed": "Не удалось выполнить запрос",
    "Return to queue": "Вернуться в очередь",
    "Published": "Опубликовано",
    "Source article": "Статья-источник",
    "Open source": "Открыть источник",
    "Source article text": "Текст статьи-источника",
    "Telegram preview": "Предпросмотр в Telegram",
    "{count} / 700 body characters": "Текст: {count} из 700 символов",
    "Model review passed": "Модель проверила черновик",
    "Model review found issues": "Модель нашла замечания",
    "Check attribution and scope against the article before approving.": (
        "Перед одобрением сверьте с исходной статьёй, кому принадлежат утверждения и к чему они "
        "относятся."
    ),
    "Manual edit. Check this version against the source before approving.": (
        "Ручная правка. Перед одобрением сверьте эту версию с источником."
    ),
    "Approve draft": "Одобрить черновик",
    "Reject": "Отклонить",
    "Regenerate": "Сгенерировать заново",
    "Edit as new revision": "Редактировать в новой версии",
    "Headline": "Заголовок",
    "Body": "Текст поста",
    "{count} / 700 characters": "{count} из 700 символов",
    "Body must contain at most 700 characters.": "Текст может содержать не более 700 символов.",
    "Save revision": "Сохранить версию",
    "Schedule this revision": "Запланировать эту версию",
    "Local date and time": "Местные дата и время",
    "Time zone": "Часовой пояс",
    "IANA name, for example Europe/Minsk. Stored in UTC.": (
        "Часовой пояс IANA, например Europe/Minsk. Время хранится в UTC."
    ),
    "Schedule publication": "Запланировать публикацию",
    "No draft for this candidate yet.": "Для этого кандидата пока нет черновика.",
    "Score and draft": "Оценить и подготовить черновик",
    "Configure model profiles to process articles here.": (
        "Настройте профили моделей, чтобы обрабатывать статьи в панели."
    ),
    "Editorial score": "Редакторская оценка",
    "cutoff": "порог",
    "Relevance": "Актуальность",
    "Importance": "Важность",
    "Novelty": "Новизна",
    "Source quality": "Качество источника",
    "Revision history": "История версий",
    "No revisions yet.": "Пока нет версий.",
    "Model calls": "Запросы к моделям",
    "this revision": "эта версия",
    "Original": "Исходная",
    "Generated": "Сгенерирована",
    "Edited": "Отредактирована",
    "Regenerated": "Сгенерирована заново",
    "Eligible": "Готов к оценке",
    "Review pending": "Ждёт проверки",
    "Approved": "Одобрен",
    "Rejected": "Отклонён",
    "Discovered": "Найден",
    "Normalized": "Обработан",
    "Skipped": "Пропущен",
    "Scored": "Оценён",
    "Selected": "Отобран",
    "scheduled": "запланировано",
    "publishing": "публикуется",
    "published": "опубликовано",
    "failed": "ошибка",
    "uncertain": "доставка не подтверждена",
    "retry wait": "ожидает повтора",
    "Scheduled for": "Запланировано на",
    "Earliest retry": "Повтор не раньше",
    "Telegram rate limit": "Telegram ограничил частоту запросов",
    "Could not connect to Telegram": "Не удалось подключиться к Telegram",
    "Telegram rejected the message": "Telegram отклонил сообщение",
    "No confirmation from Telegram": "Telegram не подтвердил отправку",
    "Telegram returned no usable message ID": "Telegram не вернул корректный ID сообщения",
    "The delivery attempt timed out": "Время ожидания отправки истекло",
    "The worker stopped before recording the result": "Воркер остановился до записи результата",
    "The message is empty": "Текст сообщения пуст",
    "The message exceeds the Telegram limit": "Текст сообщения превышает лимит Telegram",
    "See the worker log for the delivery reason": "Подробности есть в журнале воркера",
    "Check the channel for this post before requesting another attempt.": (
        "Проверьте, появился ли пост в канале, прежде чем повторять отправку."
    ),
    "Check the delivery reason before scheduling again.": (
        "Перед повторным планированием проверьте причину сбоя."
    ),
    "cancelled": "отменено",
    "Scheduled revision": "Версия в расписании",
    "Manage publication": "Управление публикацией",
    "Reschedule": "Перенести",
    "Cancel publication": "Отменить публикацию",
    "Telegram channel": "Канал Telegram",
    "Destination not confirmed": "Канал не подтверждён",
    "Confirm destination": "Подтвердить канал",
    "Configure the Telegram channel before scheduling": (
        "Настройте канал Telegram перед планированием"
    ),
    "The configured channel changed. Reload the page and try again.": (
        "Канал в настройках изменился. Обновите страницу и попробуйте снова."
    ),
    "This publication already has a destination": "Для публикации уже выбран канал",
    "Publication not found": "Публикация не найдена",
    "This candidate already has an active publication": (
        "У этой новости уже есть активная публикация"
    ),
    "The worker has started sending this publication": "Воркер уже начал отправку публикации",
    "This publication can no longer be changed": "Эту публикацию уже нельзя изменить",
    "Not Found": "Страница не найдена",
    "Method Not Allowed": "Метод запроса не поддерживается",
    "The sign-in form expired. Please try again.": (
        "Время действия формы входа истекло. Попробуйте снова."
    ),
    "The password was not accepted.": "Неверный пароль.",
    "Invalid queue filter": "Неверный фильтр очереди",
    "The form has expired. Reload this page and try again.": (
        "Время действия формы истекло. Обновите страницу и попробуйте снова."
    ),
    "The database is unavailable. Check the migrations and try again.": (
        "База данных недоступна. Проверьте миграции и попробуйте снова."
    ),
    "Candidate or draft revision not found": "Кандидат или версия черновика не найдены",
    "Source not found": "Источник не найден",
    "Unknown draft action": "Неизвестное действие с черновиком",
    "Unknown source action": "Неизвестное действие с источником",
    "A model profile is required to process candidates.": (
        "Для обработки кандидатов нужен профиль модели."
    ),
    "A model profile is required to regenerate a draft.": (
        "Для повторной генерации нужен профиль модели."
    ),
    "Headline must fit on one line.": "Заголовок должен занимать одну строку.",
    "Enter a headline of at most 120 characters and a body of at most 700.": (
        "Введите заголовок до 120 символов и текст до 700 символов."
    ),
    "This revision is already scheduled for that time": (
        "Эта версия уже запланирована на указанное время"
    ),
    "Enter a local date and time and a valid IANA time zone.": (
        "Введите местные дату и время и корректное имя часового пояса IANA."
    ),
    "The demo uses a fixed local dataset.": "Демо использует фиксированный локальный набор данных.",
    "Use a feed URL and at most 11 additional hostnames.": (
        "Укажите адрес ленты и до 11 дополнительных доменов."
    ),
    "Enter hostnames without paths, ports or credentials.": (
        "Укажите имена доменов без путей, портов и учётных данных."
    ),
    "Expected an HTML form": "Ожидались данные HTML-формы",
    "Form is too large": "Данные формы слишком велики",
    "Invalid form": "Некорректная форма",
    "Duplicate form fields": "В форме есть повторяющиеся поля",
    "Too many sign-in attempts. Try again in a minute.": (
        "Слишком много попыток входа. Попробуйте через минуту."
    ),
}


def translator(language: str) -> Callable[..., str]:
    def translate(message: str, **values: object) -> str:
        text = RUSSIAN.get(message, message) if language == "ru" else message
        return text.format(**values) if values else text

    return translate
