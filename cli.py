#!/usr/bin/env python3
"""CLI: разговор с агентом, управление слоями памяти и разбор того, что куда попало.

Быстрый старт:
    python cli.py                                     # диалог
    python cli.py "Куда переедет схема gissys?"       # один вопрос
    python cli.py --память                            # что лежит в каждом слое
    python cli.py --трейс "вопрос"                    # ответ вместе с разбором промпта
    python cli.py --слои долго "вопрос"               # только долговременная память
    python cli.py --новая-задача перенос-моделей --название "перенос схемы в GeoDjango"
    python cli.py --задача перенос-моделей --план

Полная справка: python cli.py --help

Этот файл — интерфейс. Он ничего не знает ни про LLM, ни про устройство
хранилищ: только вызывает публичные методы агента и рисует результат.
"""

import argparse
import logging
import sys

from agent import AgentError, MemoryAgent
from agent import catalog, tokens
from agent.builder import POLICY
from agent.memory.manager import ASK, AUTO, LONG, OFF, SHORT, WORKING
from agent.memory.working import STAGES

LINE = "-" * 78

# Как слои называются в командной строке. Полные имена длинны для набора,
# поэтому у каждого есть короткий синоним.
СЛОИ = {
    "кратко": SHORT, "краткосрочная": SHORT, "диалог": SHORT,
    "рабочая": WORKING, "задача": WORKING,
    "долго": LONG, "долговременная": LONG, "проект": LONG,
}

_МОДЕЛИ = "\n".join(
    f"  {м['key']:<13} {м['provider']:<9} {м['label']:<18} "
    f"окно {tokens.format_tokens(м['context_window']):>7} "
    f"{'бесплатно' if м['free'] else м['price_text']}"
    for м in catalog.describe()
)

_РОЛИ = "\n".join(
    f"  {р['role']:<16} {', '.join(р['models']):<26} {р['note']}"
    for р in catalog.describe_roles()
)

_ПОЛИТИКА = "\n".join(
    f"  {с:<11} знаний до {п['знания']}, решений до {п['решения']}, "
    f"план {'да' if п['план'] else 'нет'}, собранное {'да' if п['собрано'] else 'нет'}, "
    f"стиль {'да' if п['стиль'] else 'нет'}"
    for с, п in POLICY.items()
)

EPILOG = f"""\
слои памяти (--слои):
  кратко   краткосрочная — реплики текущего диалога, SQLite «memory/dialog.db»
  рабочая  рабочая       — состояние текущей задачи, JSON в «memory/working/»
  долго    долговременная — профиль, решения и знания в «memory/long/<кто>/»

  Перечисляются через запятую: --слои кратко,долго. Значение «все» включает
  все три, «нет» — ни одного. Выключение слоя нужно не для экономии, а для
  проверки: один и тот же вопрос с разным набором слоёв показывает, что
  именно даёт каждый из них. Это и делает compare.py.

стадии задачи (--стадия):
  planning -> execution -> validation -> done
  Разрешены только эти переходы плюс возвраты execution->planning и
  validation->execution. Остальное агент отклоняет — стадию меняет код, а не
  уверенность модели в том, что всё готово.

что берётся из памяти на каждой стадии:
{_ПОЛИТИКА}

  Факты о системе нужны, когда составляют план, и мешают, когда проверяют
  готовый код. Поэтому отбор зависит от стадии, а не только от запроса.

маршрутизация свободных реплик (--маршрутизатор):
  авто      дешёвая модель решает, стоит ли сохранить сказанное в профиль,
            знания или решения, и делает это сама выше порога уверенности
  спросить  то же, но запись только предлагается — решаете вы
  выкл      не звать модель вовсе; в долговременную память тогда пишут
            только явные команды

  Порог задаётся ключом --порог (по умолчанию 0.6). Смысл порога в том, что
  цена ошибки несимметрична: пропущенный факт легко дописать руками, а мусор
  в профиле будет подмешиваться в каждый следующий запрос.

роли моделей (какая модель на какой точке вызова):
{_РОЛИ}

  Жёсткие инварианты (стек, БД, секреты в URL) в этом списке отсутствуют
  намеренно: их проверяет код, а не модель. Модель, которую просят «проверь,
  нет ли тут Laravel», ошибается и поддаётся уговорам.

модели (--модель):
{_МОДЕЛИ}

  Без --модель агент берёт модель по роли: на планировании и на исполнении
  они разные. Явная --модель отменяет роли и фиксирует одну на всё.

примеры:
  python cli.py --память
  python cli.py --файлы
  python cli.py --трейс "Как перенести права организаций из gissys?"
  python cli.py --слои кратко "Как перенести права организаций из gissys?"
  python cli.py --запомни знания "В gisdata 37 таблиц" --ключ состав-gisdata
  python cli.py --новая-задача перенос-моделей --название "схема gissys в GeoDjango"
  python cli.py --задача перенос-моделей --план
  python cli.py --задача перенос-моделей --стадия execution "Напиши модель Organization"
  python cli.py --задача перенос-моделей --завершить
  python cli.py --журнал
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Агент с явной моделью памяти: краткосрочная, рабочая, долговременная.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("вопрос", nargs="*", help="текст вопроса; без него — диалог")

    parser.add_argument("--модель", dest="model", default="", metavar="КЛЮЧ",
                        choices=[""] + list(catalog.MODELS),
                        help="зафиксировать модель вместо выбора по роли")
    parser.add_argument("--кто", dest="user", default="инженер", metavar="ИМЯ",
                        help="пользователь: у каждого свой профиль, решения и знания")
    parser.add_argument("--сессия", dest="session", default="основная", metavar="ИМЯ",
                        help="имя диалога в краткосрочной памяти")
    parser.add_argument("--память-в", dest="base_dir", default="", metavar="ПУТЬ",
                        help="каталог с памятью (по умолчанию «memory»)")

    parser.add_argument("--слои", dest="layers", default="все", metavar="СПИСОК",
                        help="какие слои читать: кратко,рабочая,долго | все | нет")
    parser.add_argument("--маршрутизатор", dest="router", default=AUTO,
                        choices=[AUTO, ASK, OFF],
                        help="как разбирать свободные реплики (по умолчанию авто)")
    parser.add_argument("--порог", dest="threshold", type=float, default=0.6, metavar="0..1",
                        help="уверенность, ниже которой запись не делается")

    parser.add_argument("--задача", dest="task", default="", metavar="ID",
                        help="взять задачу из рабочей памяти")
    parser.add_argument("--новая-задача", dest="new_task", default="", metavar="ID",
                        help="завести задачу (стадия planning)")
    parser.add_argument("--название", dest="title", default="", metavar="ТЕКСТ",
                        help="название новой задачи")
    parser.add_argument("--стадия", dest="stage", default="", metavar="ЭТАП",
                        choices=[""] + list(STAGES),
                        help="перевести задачу на стадию: " + ", ".join(STAGES))
    parser.add_argument("--план", dest="do_plan", action="store_true",
                        help="составить план задачи и положить в рабочую память")
    parser.add_argument("--шаг", dest="step", default="", metavar="КЛЮЧ=ЗНАЧЕНИЕ",
                        help="записать промежуточный результат в рабочую память")
    parser.add_argument("--завершить", dest="finish", action="store_true",
                        help="свернуть задачу в журнал решений и очистить рабочую память")
    parser.add_argument("--задачи", dest="show_tasks", action="store_true",
                        help="список задач рабочей памяти")

    parser.add_argument("--запомни", dest="remember", nargs=2, default=[],
                        metavar=("СЛОЙ", "ТЕКСТ"),
                        help="явная запись: профиль | знания | решения | рабочая")
    parser.add_argument("--ключ", dest="key", default="", metavar="ИМЯ",
                        help="имя записи для --запомни")
    parser.add_argument("--раздел", dest="section", default="", metavar="ИМЯ",
                        help="раздел профиля для --запомни: стиль | ограничения | контекст")

    parser.add_argument("--память", dest="show_memory", action="store_true",
                        help="что лежит в каждом слое")
    parser.add_argument("--профиль", dest="show_profile", action="store_true",
                        help="показать профиль и инварианты")
    parser.add_argument("--знания", dest="show_knowledge", action="store_true",
                        help="показать факты о системе")
    parser.add_argument("--решения", dest="show_decisions", action="store_true",
                        help="показать журнал решений")
    parser.add_argument("--журнал", dest="show_journal", action="store_true",
                        help="журнал маршрутизации: что и почему куда попало")
    parser.add_argument("--файлы", dest="show_files", action="store_true",
                        help="где физически лежит каждый слой")
    parser.add_argument("--роли", dest="show_roles", action="store_true",
                        help="распределение моделей по точкам вызова")
    parser.add_argument("--трейс", dest="trace", action="store_true",
                        help="после ответа показывать разбор промпта по слоям")
    parser.add_argument("--мягкая-проверка", dest="soft", action="store_true",
                        help="дополнительно проверять стиль ответа дешёвой моделью")
    parser.add_argument("--забыть-диалог", dest="forget", action="store_true",
                        help="стереть краткосрочную память сессии (остальное цело)")
    parser.add_argument("--логи", action="store_true", help="подробный журнал работы")
    return parser


# --- разбор аргументов --------------------------------------------------------

def разобрать_слои(значение: str) -> set[str]:
    значение = (значение or "все").strip().lower()
    if значение in ("все", "всё", "all"):
        return {SHORT, WORKING, LONG}
    if значение in ("нет", "ничего", "none"):
        return set()
    итог = set()
    for кусок in значение.replace(" ", "").split(","):
        if not кусок:
            continue
        if кусок not in СЛОИ:
            raise SystemExit(
                f"Неизвестный слой «{кусок}». Допустимы: {', '.join(sorted(set(СЛОИ)))}, все, нет."
            )
        итог.add(СЛОИ[кусок])
    return итог


# --- вывод --------------------------------------------------------------------

def показать_память(agent: MemoryAgent) -> int:
    сводка = agent.stats()
    print(LINE)
    print("СЛОИ ПАМЯТИ")
    print(LINE)
    коротко = сводка[SHORT]
    print(f"  краткосрочная   реплик {коротко['реплик']}, символов {коротко['символов']}"
          f"  (сессия «{agent.session}»)")
    рабочая = сводка[WORKING]
    print(f"  рабочая         задач в работе: {рабочая['задач']}")
    for задача in agent.tasks():
        print(f"      {задача['task_id']:<24} {задача['stage_label']:<12} "
              f"план {задача['plan_steps']}, собрано {задача['collected']}")
    долго = сводка[LONG]
    print(f"  долговременная  профиль {долго['профиль']} записей, "
          f"инвариантов {долго['инвариантов']}, решений {долго['решений']}, "
          f"знаний {долго['знаний']}")
    print(LINE)
    print("Включены сейчас:", ", ".join(sorted(agent.layers)) or "ни одного")
    return 0


def показать_профиль(agent: MemoryAgent) -> int:
    профиль = agent.memory.long.profile.load()
    print(LINE)
    print(f"ПРОФИЛЬ «{agent.user_id}»")
    print(LINE)
    if профиль.get("контекст"):
        print("Зачем:", профиль["контекст"])
        print()
    for раздел in ("ограничения", "стиль"):
        записи = профиль.get(раздел) or {}
        if записи:
            print(f"{раздел.capitalize()}:")
            for ключ, значение in записи.items():
                print(f"  {ключ:<14} {значение}")
            print()
    правила = профиль.get("инварианты") or []
    print(f"Инварианты ({len(правила)}):")
    for правило in правила:
        как = правило.get("тип")
        проверка = "кодом" if как != "мягкий" else "моделью (замечание, не запрет)"
        print(f"  [{правило.get('код')}] {правило.get('правило')}")
        print(f"      проверяется {проверка}"
              + (f"; ищем: {', '.join(правило.get('значения', []))}" if правило.get("значения") else ""))
    return 0


def показать_знания(agent: MemoryAgent) -> int:
    факты = agent.memory.long.knowledge.all()
    print(LINE)
    print(f"ЗНАНИЯ О СИСТЕМЕ — {len(факты)} записей")
    print(LINE)
    for факт in факты:
        print(f"[{факт['id']}] {факт.get('тема', '')}")
        print(f"  {факт['текст']}")
        print(f"  теги: {', '.join(факт.get('теги', [])) or '—'}")
        print()
    return 0


def показать_решения(agent: MemoryAgent) -> int:
    записи = agent.memory.long.decisions.all()
    print(LINE)
    print(f"ЖУРНАЛ РЕШЕНИЙ — {len(записи)} записей")
    print(LINE)
    for запись in записи:
        print(f"{запись['id']}. {запись['заголовок']}   [{запись.get('источник', '')}]")
        print(f"   {запись['решение']}")
        if запись.get("причина"):
            print(f"   Почему: {запись['причина']}")
        for альтернатива in запись.get("альтернативы", []):
            print(f"   Отклонено: {альтернатива}")
        print()
    return 0


def показать_журнал(agent: MemoryAgent, limit: int = 25) -> int:
    записи = agent.journal(limit)
    print(LINE)
    print(f"ЖУРНАЛ МАРШРУТИЗАЦИИ — последние {len(записи)} решений")
    print(LINE)
    if not записи:
        print("Пусто: агент ещё ничего никуда не записывал.")
        return 0
    print(f"{'правило':<18} {'слой':<15} {'?':<3} текст")
    for запись in записи:
        метка = "+" if запись["применено"] else "-"
        слой = запись["слой"] + (f"/{запись['подслой']}" if запись["подслой"] else "")
        текст = (запись["текст"] or "").replace("\n", " ")[:46]
        print(f"{запись['правило']:<18} {слой:<15} {метка:<3} {текст}")
        уверенность = (f", уверенность {запись['уверенность']}"
                       if запись.get("уверенность") is not None else "")
        модель = f", модель {запись['модель']}" if запись.get("модель") else ""
        if запись.get("причина"):
            print(f"{'':<18} {'':<15}    почему: {запись['причина']}{уверенность}{модель}")
    print(LINE)
    print("«+» — запись сделана, «-» — предложение отклонено (и видно, почему).")
    return 0


def показать_файлы(agent: MemoryAgent) -> int:
    print(LINE)
    print("ГДЕ ЧТО ЛЕЖИТ")
    print(LINE)
    for имя, путь in agent.files().items():
        print(f"  {имя:<28} {путь}")
    print(LINE)
    print("Три слоя — три разных хранилища, и это видно обычным ls.")
    return 0


def показать_роли() -> int:
    print(LINE)
    print("РАСПРЕДЕЛЕНИЕ МОДЕЛЕЙ ПО ТОЧКАМ ВЫЗОВА")
    print(LINE)
    for роль in catalog.describe_roles():
        доступность = "" if catalog.get(роль["chosen"]).has_key else "  (ключа нет!)"
        print(f"  {роль['role']:<16} {', '.join(роль['models'])}{доступность}")
        print(f"      {роль['note']}")
        print(f"      почему: {роль['why']}")
    print(LINE)
    print("  жёсткие инварианты   без модели — статическая проверка кодом")
    print("      Должно быть детерминировано на 100%, а модель поддаётся уговорам.")
    print(LINE)
    print("Лестница эскалации:", " -> ".join(catalog.ESCALATION))
    return 0


def показать_задачи(agent: MemoryAgent) -> int:
    задачи = agent.tasks()
    print(LINE)
    print(f"ЗАДАЧИ В РАБОЧЕЙ ПАМЯТИ — {len(задачи)}")
    print(LINE)
    if not задачи:
        print("Пусто. Завести: --новая-задача ИД --название «...».")
        return 0
    for задача in задачи:
        print(f"  {задача['task_id']:<26} {задача['stage_label']:<12} "
              f"план {задача['plan_steps']:<3} собрано {задача['collected']:<3} "
              f"{задача['title']}")
    return 0


def показать_трейс(ответ) -> None:
    """Разбор промпта: какой слой что дал и почему."""
    print()
    print(LINE)
    print("ЧТО УШЛО В ПРОМПТ")
    print(LINE)
    for блок in ответ.trace():
        метка = "+" if блок["включён"] else "-"
        print(f" {метка} {блок['слой']:<15} {блок['блок']:<20} "
              f"{tokens.format_tokens(блок['токенов']):>6} т.  {блок['почему']}")
        if блок["включён"] and блок["записи"]:
            for запись in блок["записи"][:6]:
                print(f"       · {str(запись)[:88]}")
            если_ещё = len(блок["записи"]) - 6
            if если_ещё > 0:
                print(f"       · …и ещё {если_ещё}")
    print(LINE)
    итог = ответ.layers()
    for слой, данные in итог.items():
        print(f"  {слой:<15} блоков {данные['блоков']}, записей {данные['записей']}, "
              f"токенов {tokens.format_tokens(данные['токенов'])}")
    if ответ.routing_entry:
        запись = ответ.routing_entry
        решение = "записано" if запись["применено"] else "не записано"
        print(f"  маршрутизатор: {решение} -> {запись['слой']}"
              f"{'/' + запись['подслой'] if запись['подслой'] else ''}; {запись['причина']}")
    if ответ.violations:
        print(f"  НАРУШЕНИЯ ИНВАРИАНТОВ: {len(ответ.violations)} "
              f"(попыток {ответ.attempts})")
        for нарушение in ответ.violations:
            print(f"       · {нарушение}")
    elif ответ.attempts > 1:
        print(f"  инварианты: нарушение исправлено с попытки {ответ.attempts}"
              + (f", эскалация на {ответ.escalated_to}" if ответ.escalated_to else ""))
    if ответ.soft:
        мягкая = ответ.soft
        print(f"  мягкая проверка ({мягкая.model_key}): "
              f"{'годится' if мягкая.ok else 'замечания'} — {'; '.join(мягкая.notes) or 'нет'}")
    print(LINE)


def показать_расход(ответ) -> None:
    расход = ответ.usage
    print(f"[{расход.get('model_key', '')}: {tokens.format_tokens(расход.get('total_tokens', 0))} "
          f"токенов, {tokens.format_cost(расход.get('cost', 0.0))}, "
          f"{расход.get('elapsed', 0)} с]")


# --- диалог -------------------------------------------------------------------

ПОДСКАЗКА = """\
Команды в диалоге:
  память              что лежит в каждом слое
  журнал              что и почему куда записалось
  профиль | знания | решения | задачи
  трейс               включить/выключить разбор промпта
  слои кратко,долго   поменять набор читаемых слоёв
  стадия execution    перевести задачу на стадию
  план                составить план задачи
  шаг ключ=значение   записать промежуточный результат
  завершить           свернуть задачу в журнал решений
  помощь | выход
"""


def диалог(agent: MemoryAgent, трейс: bool) -> int:
    print(LINE)
    инфо = agent.info()
    print(f"Агент миграции ГИС. Пользователь «{инфо['user_id']}», сессия «{инфо['session']}».")
    print(f"Слои: {', '.join(инфо['layers']) or 'ни одного'}. "
          f"Маршрутизатор: {инфо['router_mode']}. Модель: {инфо['model_key']}.")
    if инфо["task"]:
        print(f"Задача «{инфо['task']['task_id']}», стадия {инфо['stage']} "
              f"(дальше можно: {', '.join(инфо['allowed']) or 'только завершить'}).")
    print("«помощь» — список команд, «выход» — закончить.")
    print(LINE)

    while True:
        try:
            строка = input("\nВы: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nДо встречи.")
            return 0
        if not строка:
            continue
        нижний = строка.lower()

        if нижний in ("выход", "quit", "exit"):
            print("До встречи.")
            return 0
        if нижний in ("помощь", "?", "help"):
            print(ПОДСКАЗКА)
            continue
        if нижний == "память":
            показать_память(agent)
            continue
        if нижний == "журнал":
            показать_журнал(agent)
            continue
        if нижний == "профиль":
            показать_профиль(agent)
            continue
        if нижний == "знания":
            показать_знания(agent)
            continue
        if нижний == "решения":
            показать_решения(agent)
            continue
        if нижний == "задачи":
            показать_задачи(agent)
            continue
        if нижний == "трейс":
            трейс = not трейс
            print(f"Разбор промпта {'включён' if трейс else 'выключен'}.")
            continue
        if нижний.startswith("слои "):
            try:
                agent.set_layers(разобрать_слои(строка[5:]))
            except (SystemExit, AgentError) as exc:
                print(f"Ошибка: {exc}")
                continue
            print(f"Читаются слои: {', '.join(sorted(agent.layers)) or 'ни одного'}.")
            continue
        if нижний.startswith("стадия "):
            try:
                задача = agent.transition(строка[7:].strip())
            except AgentError as exc:
                print(f"Отклонено: {exc}")
                continue
            print(f"Стадия: {задача.stage} ({задача.stage_label}). "
                  f"Дальше можно: {', '.join(задача.allowed()) or 'только завершить'}.")
            continue
        if нижний.startswith("шаг "):
            ключ, _, значение = строка[4:].partition("=")
            if not значение.strip():
                print("Нужно «шаг ключ=значение».")
                continue
            try:
                agent.remember_step(ключ.strip(), значение.strip())
            except AgentError as exc:
                print(f"Ошибка: {exc}")
                continue
            print(f"В рабочую память задачи: {ключ.strip()}.")
            continue
        if нижний == "план":
            try:
                ответ = agent.plan()
            except AgentError as exc:
                print(f"Ошибка: {exc}")
                continue
            print(f"\nАгент:\n{ответ.text}")
            if agent.task and agent.task.plan:
                print(f"\nВ рабочую память записано шагов: {len(agent.task.plan)}.")
            if трейс:
                показать_трейс(ответ)
            показать_расход(ответ)
            continue
        if нижний == "завершить":
            try:
                запись = agent.finish_task()
            except AgentError as exc:
                print(f"Ошибка: {exc}")
                continue
            print(f"Задача свёрнута в решение №{запись['id']}: {запись['заголовок']}")
            print(f"  {запись['решение'][:400]}")
            continue

        try:
            ответ = agent.ask(строка)
        except AgentError as exc:
            print(f"Ошибка: {exc}")
            continue
        print(f"\nАгент: {ответ.text}")
        if ответ.blocked:
            print("\n! Ответ нарушает инварианты и не был исправлен за отведённые попытки.")
        if трейс:
            показать_трейс(ответ)
        показать_расход(ответ)


# --- точка входа --------------------------------------------------------------

def main() -> int:
    аргументы = build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO if аргументы.логи else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if аргументы.show_roles:
        return показать_роли()

    слои = разобрать_слои(аргументы.layers)

    try:
        agent = MemoryAgent(
            model_key=аргументы.model,
            user_id=аргументы.user,
            session=аргументы.session,
            base_dir=аргументы.base_dir,
            layers=слои,
            router_mode=аргументы.router,
            threshold=аргументы.threshold,
            soft_check=аргументы.soft,
        )
    except AgentError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    try:
        return выполнить(agent, аргументы)
    except AgentError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    finally:
        agent.close()


def выполнить(agent: MemoryAgent, аргументы) -> int:
    # Команда, которая что-то изменила и не задала вопроса, на этом и
    # заканчивается. Иначе «--стадия validation» молча открывал бы диалог, и
    # в скрипте это выглядит как зависший вызов.
    изменяющие = bool(
        аргументы.new_task or аргументы.stage or аргументы.step or аргументы.remember
    )

    if аргументы.forget:
        стёрто = agent.memory.short.clear(agent.session)
        print(f"Краткосрочная память сессии «{agent.session}» очищена: реплик стёрто {стёрто}.")
        print("Рабочая и долговременная память не тронуты — это разные слои.")
        return 0

    if аргументы.new_task:
        задача = agent.start_task(аргументы.new_task, аргументы.title)
        print(f"Задача «{задача.task_id}» заведена, стадия {задача.stage}.")
    elif аргументы.task:
        задача = agent.use_task(аргументы.task)
        print(f"Задача «{задача.task_id}», стадия {задача.stage} ({задача.stage_label}).")

    if аргументы.stage:
        задача = agent.transition(аргументы.stage)
        print(f"Стадия: {задача.stage}. Дальше можно: "
              f"{', '.join(задача.allowed()) or 'только завершить'}.")

    if аргументы.step:
        ключ, _, значение = аргументы.step.partition("=")
        if not значение.strip():
            print("Нужно «--шаг ключ=значение».", file=sys.stderr)
            return 1
        agent.remember_step(ключ.strip(), значение.strip())
        print(f"В рабочую память задачи записано: {ключ.strip()}.")

    if аргументы.remember:
        слой, текст = аргументы.remember
        запись = agent.remember(слой, текст, key=аргументы.key, section=аргументы.section)
        print(f"Записано в {запись['слой']}"
              f"{'/' + запись['подслой'] if запись['подслой'] else ''} по правилу "
              f"«{запись['правило']}».")

    if аргументы.show_files:
        return показать_файлы(agent)
    if аргументы.show_memory:
        return показать_память(agent)
    if аргументы.show_profile:
        return показать_профиль(agent)
    if аргументы.show_knowledge:
        return показать_знания(agent)
    if аргументы.show_decisions:
        return показать_решения(agent)
    if аргументы.show_journal:
        return показать_журнал(agent)
    if аргументы.show_tasks:
        return показать_задачи(agent)

    if аргументы.do_plan:
        ответ = agent.plan()
        print(f"\n{ответ.text}\n")
        if agent.task and agent.task.plan:
            print(f"В рабочую память записано шагов: {len(agent.task.plan)}.")
        if аргументы.trace:
            показать_трейс(ответ)
        показать_расход(ответ)
        return 0

    if аргументы.finish:
        запись = agent.finish_task()
        print(f"Задача свёрнута в решение №{запись['id']}: {запись['заголовок']}")
        print(запись["решение"])
        if запись.get("причина"):
            print(f"Почему: {запись['причина']}")
        print("Рабочая память задачи очищена — итог теперь живёт в журнале решений.")
        return 0

    вопрос = " ".join(аргументы.вопрос).strip()
    if not вопрос:
        if изменяющие:
            return 0
        return диалог(agent, аргументы.trace)

    ответ = agent.ask(вопрос)
    print(ответ.text)
    if ответ.blocked:
        print("\n! Ответ нарушает инварианты и не был исправлен за отведённые попытки.")
    if аргументы.trace:
        показать_трейс(ответ)
    показать_расход(ответ)
    return 0


if __name__ == "__main__":
    sys.exit(main())
