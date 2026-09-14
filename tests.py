#!/usr/bin/env python3
"""Тесты модели памяти. По умолчанию без сети.

    python tests.py              — все тесты без обращений к API
    python tests.py --живые      — плюс проверки, которым нужен реальный ключ
    python tests.py -v           — подробный вывод

Сетевых вызовов в основном наборе нет намеренно: правила маршрутизации, границы
слоёв и проверка инвариантов — это код, и он должен проверяться без оглядки на
доступность провайдера и на лимиты бесплатного тарифа.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import catalog, seed as seed_module
from agent.builder import POLICY, PromptBuilder
from agent.memory.long import LongTermError, LongTermMemory
from agent.memory.manager import LONG, OFF, SHORT, WORKING, MemoryManager
from agent.memory.router import Routing, _parse
from agent.memory.short import ShortTermMemory
from agent.memory.working import (
    DONE, EXECUTION, PLANNING, VALIDATION, TaskState, TransitionError, WorkingMemory,
    WorkingMemoryError,
)
from agent.validator import StateValidator

ЖИВЫЕ = "--живые" in sys.argv
if ЖИВЫЕ:
    sys.argv.remove("--живые")


class ВременнаяПамять(unittest.TestCase):
    """Общий каркас: каждый тест работает на своей копии памяти."""

    def setUp(self) -> None:
        self.каталог = tempfile.mkdtemp(prefix="тест-памяти-")
        self.память = MemoryManager(base_dir=self.каталог, router_mode=OFF)

    def tearDown(self) -> None:
        shutil.rmtree(self.каталог, ignore_errors=True)


# --- краткосрочная память -----------------------------------------------------

class КраткосрочнаяПамять(unittest.TestCase):

    def setUp(self) -> None:
        self.память = ShortTermMemory(":memory:")

    def test_окно_ограничено_числом_сообщений(self):
        for i in range(20):
            self.память.append("с", "user" if i % 2 == 0 else "assistant", f"реплика {i}")
        окно = self.память.window("с", max_messages=6)
        self.assertEqual(len(окно), 6)
        self.assertEqual(окно[-1]["content"], "реплика 19")

    def test_окно_ограничено_символами(self):
        self.память.append("с", "user", "х" * 5000)
        self.память.append("с", "assistant", "короткая")
        окно = self.память.window("с", max_messages=10, max_chars=1000)
        # Первая реплика не влезает по символам, но одна запись остаётся всегда:
        # пустое окно хуже, чем окно из одного сообщения.
        self.assertEqual(len(окно), 1)
        self.assertEqual(окно[0]["content"], "короткая")

    def test_сессии_не_смешиваются(self):
        self.память.append("работа", "user", "про работу")
        self.память.append("черновик", "user", "про черновик")
        self.assertEqual(len(self.память.all("работа")), 1)
        self.assertEqual(len(self.память.all("черновик")), 1)

    def test_пустая_реплика_не_сохраняется(self):
        with self.assertRaises(Exception):
            self.память.append("с", "user", "   ")

    def test_неизвестная_роль_отклоняется(self):
        with self.assertRaises(Exception):
            self.память.append("с", "system", "текст")


# --- рабочая память -----------------------------------------------------------

class РабочаяПамять(unittest.TestCase):

    def setUp(self) -> None:
        self.каталог = tempfile.mkdtemp()
        self.память = WorkingMemory(self.каталог)

    def tearDown(self) -> None:
        shutil.rmtree(self.каталог, ignore_errors=True)

    def test_разрешённый_маршрут_проходит_целиком(self):
        задача = self.память.create("з1", "тест")
        for стадия in (EXECUTION, VALIDATION, DONE):
            задача.transition(стадия)
        self.assertTrue(задача.finished)
        self.assertEqual(len(задача.transitions), 3)

    def test_прыжок_через_стадию_отклоняется(self):
        задача = self.память.create("з2")
        with self.assertRaises(TransitionError):
            задача.transition(DONE)
        self.assertEqual(задача.stage, PLANNING)

    def test_возвраты_разрешены(self):
        задача = self.память.create("з3")
        задача.transition(EXECUTION)
        задача.transition(PLANNING)          # план оказался негодным
        задача.transition(EXECUTION)
        задача.transition(VALIDATION)
        задача.transition(EXECUTION)         # нашли дефект
        self.assertEqual(задача.stage, EXECUTION)

    def test_из_done_никуда(self):
        задача = self.память.create("з4")
        for стадия in (EXECUTION, VALIDATION, DONE):
            задача.transition(стадия)
        self.assertEqual(задача.allowed(), ())
        with self.assertRaises(TransitionError):
            задача.transition(PLANNING)

    def test_состояние_переживает_перезапуск(self):
        задача = self.память.create("з5", "перенос")
        задача.transition(EXECUTION)
        задача.remember("таблиц", "37")
        задача.set_plan(["шаг один", "шаг два"])
        self.память.save(задача)

        другая = WorkingMemory(self.каталог).load("з5")
        self.assertEqual(другая.stage, EXECUTION)
        self.assertEqual(другая.collected["таблиц"], "37")
        self.assertEqual(другая.plan, ["шаг один", "шаг два"])

    def test_повторное_создание_отклоняется(self):
        self.память.create("з6")
        with self.assertRaises(WorkingMemoryError):
            self.память.create("з6")

    def test_недопустимый_идентификатор(self):
        for плохой in ("../побег", "имя с пробелом", "", "a" * 100):
            with self.assertRaises(WorkingMemoryError):
                self.память.create(плохой)

    def test_отсутствующая_задача_даёт_понятную_ошибку(self):
        with self.assertRaises(WorkingMemoryError):
            self.память.load("нет-такой")


# --- долговременная память ----------------------------------------------------

class ДолговременнаяПамять(unittest.TestCase):

    def setUp(self) -> None:
        self.каталог = tempfile.mkdtemp()
        self.память = LongTermMemory(self.каталог, "кто-то")

    def tearDown(self) -> None:
        shutil.rmtree(self.каталог, ignore_errors=True)

    def test_три_отдельных_файла(self):
        self.память.profile.update("ограничения", "бд", "PostgreSQL")
        self.память.decisions.add("Стек", "Django")
        self.память.knowledge.add("ф1", "факт")
        пути = set(self.память.files().values())
        self.assertEqual(len(пути), 3)
        for путь in пути:
            self.assertTrue(os.path.exists(путь), путь)

    def test_профиль_перезаписывается_а_решения_дописываются(self):
        self.память.profile.update("ограничения", "бд", "PostgreSQL 16")
        self.память.profile.update("ограничения", "бд", "PostgreSQL 17")
        self.assertEqual(self.память.profile.load()["ограничения"]["бд"], "PostgreSQL 17")

        self.память.decisions.add("Первое", "текст один")
        self.память.decisions.add("Второе", "текст два")
        self.assertEqual(len(self.память.decisions.all()), 2)

    def test_неизвестный_раздел_профиля(self):
        with self.assertRaises(LongTermError):
            self.память.profile.update("настроение", "тон", "бодрый")

    def test_жёсткий_инвариант_без_значений_отклоняется(self):
        # Инвариант, который нечем проверить, хуже, чем его отсутствие:
        # он создаёт ложное чувство защиты.
        with self.assertRaises(LongTermError):
            self.память.profile.add_invariant(
                {"код": "пустой", "правило": "нельзя", "тип": "запрет-слов", "значения": []}
            )

    def test_негодная_регулярка_отклоняется(self):
        with self.assertRaises(LongTermError):
            self.память.profile.add_invariant(
                {"код": "битый", "правило": "нельзя", "тип": "запрет-регулярок",
                 "значения": ["[незакрытая"]}
            )

    def test_инвариант_обновляется_по_коду(self):
        for правило in ("первая версия", "вторая версия"):
            self.память.profile.add_invariant(
                {"код": "один", "правило": правило, "тип": "мягкий"}
            )
        правила = self.память.profile.invariants()
        self.assertEqual(len(правила), 1)
        self.assertEqual(правила[0]["правило"], "вторая версия")

    def test_знания_отбираются_по_релевантности(self):
        self.память.knowledge.add("схема", "Схема gissys: account, group, organization",
                                  tags=["planning", "бд"])
        self.память.knowledge.add("фронт", "OpenLayers 2.13 рисует слои", tags=["execution"])
        отобрано = [ф["id"] for ф in self.память.knowledge.relevant("что в схеме gissys")]
        self.assertEqual(отобрано, ["схема"])

    def test_несовпавший_запрос_не_тянет_ничего(self):
        self.память.knowledge.add("схема", "Схема gissys", tags=["planning"])
        self.assertEqual(self.память.knowledge.relevant("погода в Москве"), [])

    def test_факт_уточняется_а_не_дублируется(self):
        self.память.knowledge.add("в", "PostGIS 3.4")
        self.память.knowledge.add("в", "PostGIS 3.6")
        факты = self.память.knowledge.all()
        self.assertEqual(len(факты), 1)
        self.assertEqual(факты[0]["текст"], "PostGIS 3.6")


# --- правила маршрутизации ----------------------------------------------------

class ПравилаМаршрутизации(ВременнаяПамять):

    def test_реплика_идёт_в_короткую_память(self):
        self.память.remember_message("user", "вопрос")
        self.assertEqual(self.память.stats()[SHORT]["реплик"], 1)
        последняя = self.память.journal(1)[0]
        self.assertEqual(последняя["правило"], "реплика-диалога")
        self.assertEqual(последняя["слой"], SHORT)

    def test_шаг_задачи_идёт_в_рабочую_память(self):
        задача = self.память.working.create("з", "тест")
        self.память.remember_step(задача, "таблиц", "37")
        self.assertEqual(self.память.working.load("з").collected["таблиц"], "37")
        self.assertEqual(self.память.journal(1)[0]["слой"], WORKING)

    def test_явное_указание_идёт_куда_сказано(self):
        self.память.remember_explicit("знания", "В gisdata 37 таблиц", key="состав")
        запись = self.память.journal(1)[0]
        self.assertEqual(запись["правило"], "явное-указание")
        self.assertEqual(запись["подслой"], "знания")
        self.assertEqual(len(self.память.long.knowledge.all()), 1)

    def test_явное_указание_в_неизвестный_слой_отклоняется(self):
        with self.assertRaises(LongTermError):
            self.память.remember_explicit("подсознание", "что-то")

    def test_завершение_задачи_переносит_её_в_решения(self):
        задача = self.память.working.create("з", "перенос моделей")
        self.память.remember_step(задача, "итог", "модели описаны")
        for стадия in (EXECUTION, VALIDATION):
            задача.transition(стадия)
        self.память.working.save(задача)

        запись = self.память.finish_task(задача)
        self.assertEqual(len(self.память.long.decisions.all()), 1)
        self.assertIn("перенос моделей", запись["заголовок"])
        # Рабочая память задачи очищена: её итог теперь живёт в журнале решений.
        self.assertEqual(self.память.working.tasks(), [])

    def test_очистка_диалога_не_трогает_другие_слои(self):
        self.память.remember_message("user", "реплика")
        задача = self.память.working.create("з")
        self.память.remember_step(задача, "к", "з")
        self.память.remember_explicit("знания", "факт", key="ф")

        self.память.short.clear(self.память.session)
        сводка = self.память.stats()
        self.assertEqual(сводка[SHORT]["реплик"], 0)
        self.assertEqual(сводка[WORKING]["задач"], 1)
        self.assertEqual(сводка[LONG]["знаний"], 1)

    def test_маршрутизатор_выключен_ничего_не_пишет(self):
        предложение, запись = self.память.route("Отвечай кратко")
        self.assertFalse(предложение.wants_write)
        self.assertFalse(запись["применено"])
        self.assertEqual(self.память.stats()[LONG]["профиль"], 0)

    def test_отклонённое_предложение_видно_в_журнале(self):
        # Ниже порога — записи нет, но след остаётся: потом видно, что именно
        # агент решил не запоминать.
        self.память.router_mode = "авто"
        self.память.router = _ЗаглушкаМаршрутизатора(
            Routing(target="знания", key="к", value="факт", confidence=0.3)
        )
        предложение, запись = self.память.route("какая-то реплика")
        self.assertFalse(запись["применено"])
        self.assertIn("ниже порога", запись["причина"])
        self.assertEqual(len(self.память.long.knowledge.all()), 0)

    def test_уверенное_предложение_применяется(self):
        self.память.router_mode = "авто"
        self.память.router = _ЗаглушкаМаршрутизатора(
            Routing(target="знания", key="версия", value="PostGIS 3.6", confidence=0.9)
        )
        _, запись = self.память.route("у нас PostGIS 3.6")
        self.assertTrue(запись["применено"])
        self.assertEqual(self.память.long.knowledge.all()[0]["текст"], "PostGIS 3.6")

    def test_сбой_маршрутизатора_не_ломает_запись(self):
        self.память.router_mode = "авто"
        self.память.router = _ЗаглушкаМаршрутизатора(Routing(failed=True))
        предложение, запись = self.память.route("реплика")
        self.assertTrue(предложение.failed)
        self.assertFalse(запись["применено"])


class _ЗаглушкаМаршрутизатора:
    """Маршрутизатор с заранее известным ответом — чтобы тесты не ходили в сеть."""

    def __init__(self, routing: Routing) -> None:
        self.routing = routing

    def classify(self, text: str) -> Routing:
        return self.routing


# --- разбор ответа маршрутизатора ---------------------------------------------

class РазборОтветаМодели(unittest.TestCase):

    def test_чистый_json(self):
        разбор = _parse('{"слой":"знания","ключ":"к","значение":"з","уверенность":0.8}')
        self.assertEqual(разбор.target, "знания")
        self.assertAlmostEqual(разбор.confidence, 0.8)

    def test_json_в_markdown(self):
        разбор = _parse('```json\n{"слой":"профиль","раздел":"стиль","значение":"кратко",'
                        '"уверенность":0.9}\n```')
        self.assertEqual(разбор.target, "профиль")
        self.assertEqual(разбор.section, "стиль")

    def test_json_с_болтовнёй_вокруг(self):
        разбор = _parse('Конечно! Вот ответ: {"слой":"нет","уверенность":0} — надеюсь, помог.')
        self.assertEqual(разбор.target, "нет")

    def test_хвост_после_объекта_не_мешает(self):
        # Слабые модели присылают валидный объект и следом обрывок служебного
        # тега. Срез «от первой { до последней }» на этом ломается.
        разбор = _parse('{"слой":"нет","уверенность":0}</think>{обрывок')
        self.assertEqual(разбор.target, "нет")

    def test_берётся_первый_из_двух_объектов(self):
        разбор = _parse('{"слой":"знания","значение":"факт","уверенность":0.9} {"слой":"нет"}')
        self.assertEqual(разбор.value, "факт")

    def test_скобка_внутри_строки_не_обрывает_разбор(self):
        разбор = _parse('{"слой":"знания","значение":"вот } скобка","уверенность":0.9}')
        self.assertEqual(разбор.value, "вот } скобка")

    def test_мусор_даёт_none(self):
        self.assertIsNone(_parse("я не понял вопроса"))

    def test_неизвестный_слой_даёт_none(self):
        self.assertIsNone(_parse('{"слой":"подсознание","уверенность":1}'))

    def test_уверенность_загоняется_в_границы(self):
        self.assertEqual(_parse('{"слой":"нет","уверенность":7}').confidence, 1.0)
        self.assertEqual(_parse('{"слой":"нет","уверенность":-3}').confidence, 0.0)


# --- сборка промпта -----------------------------------------------------------

class СборкаПромпта(ВременнаяПамять):

    def setUp(self) -> None:
        super().setUp()
        seed_module.seed(self.память)
        self.память.remember_message("user", "прошлая реплика")
        self.сборщик = PromptBuilder(self.память)

    def test_выключенный_слой_не_даёт_записей(self):
        промпт = self.сборщик.build("вопрос", layers={SHORT})
        self.assertNotIn(LONG, промпт.by_layer())
        причины = [б.why for б in промпт.blocks if б.layer == LONG]
        self.assertTrue(all("выключена" in п for п in причины))

    def test_инварианты_идут_всегда(self):
        for стадия in (PLANNING, EXECUTION, VALIDATION, DONE):
            задача = TaskState(task_id="з", stage=стадия)
            промпт = self.сборщик.build("вопрос", задача)
            блок = [б for б in промпт.blocks if б.name == "инварианты"][0]
            self.assertTrue(блок.included, f"инварианты пропали на стадии {стадия}")

    @staticmethod
    def _фактов(промпт) -> int:
        """Сколько записей знаний попало в промпт (0, если блок не включён)."""
        блоки = [б for б in промпт.included if б.name == "знания"]
        return len(блоки[0].entries) if блоки else 0

    def test_знания_зависят_от_стадии(self):
        # Факты о системе нужны, когда строят план, и мешают, когда проверяют
        # уже написанный код. Политика стадий именно это и задаёт.
        вопрос = "как перенести схему gissys"
        планирование = self.сборщик.build(вопрос, TaskState("з", stage=PLANNING))
        проверка = self.сборщик.build(вопрос, TaskState("з", stage=VALIDATION))
        self.assertGreater(self._фактов(планирование), self._фактов(проверка))

    def test_на_завершённой_задаче_знаний_нет(self):
        промпт = self.сборщик.build("итог?", TaskState("з", stage=DONE))
        self.assertEqual(self._фактов(промпт), 0)

    def test_стиль_опускается_на_проверке(self):
        промпт = self.сборщик.build("вопрос", TaskState("з", stage=VALIDATION))
        профиль = [б for б in промпт.blocks if б.name == "профиль"][0]
        self.assertFalse(any("стиль/" in з for з in профиль.entries))

    def test_план_и_собранное_попадают_на_исполнении(self):
        задача = TaskState("з", stage=EXECUTION, plan=["шаг раз"], collected={"к": "з"})
        промпт = self.сборщик.build("вопрос", задача)
        имена = {б.name for б in промпт.included}
        self.assertIn("план", имена)
        self.assertIn("собранные данные", имена)

    def test_трейс_объясняет_каждый_блок(self):
        промпт = self.сборщик.build("вопрос")
        for блок in промпт.blocks:
            self.assertTrue(блок.why, f"блок «{блок.name}» без объяснения")

    def test_порядок_блоков_фиксирован(self):
        промпт = self.сборщик.build("вопрос")
        имена = [б.name for б in промпт.blocks]
        self.assertLess(имена.index("инварианты"), имена.index("профиль"))
        self.assertEqual(имена[-1], "вопрос пользователя")

    def test_без_задачи_берётся_политика_планирования(self):
        промпт = self.сборщик.build("вопрос")
        self.assertEqual(промпт.stage, PLANNING)

    def test_все_стадии_описаны_политикой(self):
        for стадия in (PLANNING, EXECUTION, VALIDATION, DONE):
            self.assertIn(стадия, POLICY)


# --- проверка инвариантов -----------------------------------------------------

class ПроверкаИнвариантов(ВременнаяПамять):

    def setUp(self) -> None:
        super().setUp()
        seed_module.seed(self.память)
        self.валидатор = StateValidator(self.память.long.profile)

    def test_предложение_чужого_стека_ловится(self):
        нарушения = self.валидатор.check("Возьмём Laravel, на нём быстрее.")
        self.assertEqual(len(нарушения), 1)
        self.assertEqual(нарушения[0].code, "стек-бэкенд")

    def test_отказ_от_чужого_стека_не_считается_нарушением(self):
        чисто = self.валидатор.check(
            "Laravel здесь не подойдёт: геометрия только через сырой SQL, берём GeoDjango."
        )
        self.assertEqual(чисто, [])

    def test_упоминание_legacy_разрешено(self):
        чисто = self.валидатор.check(
            "Контроллер userpgplace.php из CodeIgniter 1 превращается в Django-вьюху."
        )
        self.assertEqual(чисто, [])

    def test_код_на_старом_стеке_ловится(self):
        нарушения = self.валидатор.check('```php\n<?php\n$this->load->model("x");\n```')
        self.assertTrue(нарушения)
        self.assertEqual(нарушения[0].where, "код")

    def test_чужая_субд_в_коде_ловится(self):
        нарушения = self.валидатор.check("```python\nDATABASES = {'ENGINE': 'mysql'}\n```")
        self.assertTrue(нарушения)

    def test_секрет_в_url_ловится(self):
        нарушения = self.валидатор.check("Дёргайте /api/export?token=abc123")
        self.assertEqual(нарушения[0].code, "секреты-в-url")

    def test_чистый_ответ_проходит(self):
        self.assertEqual(self.валидатор.check(
            "```python\nfrom django.contrib.gis.db import models\n\n"
            "class Pipe(models.Model):\n    geom = models.LineStringField(srid=3857)\n```"
        ), [])

    def test_напоминание_содержит_нарушение(self):
        нарушения = self.валидатор.check("Сделаем на Laravel.")
        напоминание = self.валидатор.reminder(нарушения)
        self.assertIn("laravel", напоминание.lower())

    def test_переход_проверяется_без_изменения_состояния(self):
        задача = TaskState("з", stage=PLANNING)
        можно, пояснение = StateValidator.check_transition(задача, DONE)
        self.assertFalse(можно)
        self.assertIn("не разрешён", пояснение)
        self.assertEqual(задача.stage, PLANNING)   # состояние не тронуто

    def test_мягкие_инварианты_не_проверяются_кодом(self):
        жёсткие = {и["код"] for и in self.память.long.profile.invariants(hard_only=True)}
        self.assertNotIn("1С-источник-истины", жёсткие)


# --- каталог моделей ----------------------------------------------------------

class КаталогМоделей(unittest.TestCase):

    def test_у_каждой_роли_есть_модель(self):
        for роль in catalog.ROLES:
            self.assertIn(catalog.for_role(роль, offset=0), catalog.MODELS)

    def test_частые_роли_чередуют_провайдеров(self):
        модели = {catalog.for_role("маршрутизация", offset=i) for i in range(2)}
        провайдеры = {catalog.get(м).provider for м in модели}
        self.assertGreater(len(провайдеры), 1, "частая роль сидит на одном провайдере")

    def test_эскалация_поднимает_на_ступень(self):
        self.assertEqual(catalog.escalate("groq-allam7b"), "groq-20b")
        self.assertEqual(catalog.escalate("groq-20b"), "groq-120b")
        self.assertEqual(catalog.escalate("groq-120b"), "ds-pro")

    def test_с_вершины_лестницы_некуда(self):
        self.assertEqual(catalog.escalate("ds-pro"), "ds-pro")

    def test_модель_вне_лестницы_идёт_на_сильную_бесплатную(self):
        self.assertEqual(catalog.escalate("groq-qwen27b"), "groq-120b")

    def test_неизвестная_роль_даёт_ошибку(self):
        with self.assertRaises(KeyError):
            catalog.for_role("телепатия")


# --- начальное наполнение -----------------------------------------------------

class НачальноеНаполнение(ВременнаяПамять):

    def test_наполняет_пустую_память(self):
        сводка = seed_module.seed(self.память)
        self.assertGreater(сводка["знания"], 0)
        self.assertEqual(self.память.long.stats()["инварианты"], len(seed_module.INVARIANTS))

    def test_не_перезаписывает_заполненную(self):
        seed_module.seed(self.память)
        self.память.long.profile.update("стиль", "тон", "мой собственный")
        seed_module.seed(self.память)
        self.assertEqual(self.память.long.profile.load()["стиль"]["тон"], "мой собственный")

    def test_жёсткие_инварианты_проверяемы(self):
        seed_module.seed(self.память)
        for инвариант in self.память.long.profile.invariants(hard_only=True):
            self.assertTrue(инвариант.get("значения"),
                            f"инвариант «{инвариант['код']}» нечем проверять")


# --- живые проверки -----------------------------------------------------------

@unittest.skipUnless(ЖИВЫЕ, "нужен ключ API; запускать с --живые")
class ЖивыеПроверки(unittest.TestCase):

    def test_маршрутизатор_отличает_вопрос_от_факта(self):
        from agent.llm import Client
        from agent.memory.router import Router
        клиент = Client()
        try:
            маршрутизатор = Router(клиент)
            вопрос = маршрутизатор.classify("А как в GeoDjango сделать индекс по геометрии?")
            факт = маршрутизатор.classify("У нас в схеме gisdata 37 таблиц")
            self.assertFalse(вопрос.wants_write, f"вопрос принят за факт: {вопрос.to_dict()}")
            self.assertTrue(факт.wants_write or факт.failed, факт.to_dict())
        finally:
            клиент.close()

    def test_агент_отвечает_и_не_нарушает_инвариантов(self):
        from agent import MemoryAgent
        каталог = tempfile.mkdtemp()
        агент = MemoryAgent(base_dir=каталог, router_mode=OFF, temperature=0.0)
        try:
            ответ = агент.ask("Какой ORM использовать для геометрии в новой системе?")
            self.assertTrue(ответ.text)
            self.assertFalse(ответ.blocked, [str(н) for н in ответ.violations])
            self.assertIn(LONG, ответ.layers())
        finally:
            агент.close()
            shutil.rmtree(каталог, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
