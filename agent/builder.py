"""PromptBuilder — единственная точка ЧТЕНИЯ памяти.

MemoryManager отвечает за то, что куда записывается. Этот модуль отвечает за
обратное: что из какого слоя попадает в запрос к модели — и, главное, делает
это видимым. Каждый собранный промпт сопровождается трейсом: какой блок из
какого слоя вошёл, сколько занял токенов, какие именно записи в нём и почему
он вообще включён (или почему исключён).

Ключевое проектное решение — отбор зависит от СТАДИИ задачи. Факты о структуре
legacy-схем нужны, когда составляют план переноса, и только мешают, когда
проверяют уже написанный код. Поэтому у каждой стадии своя политика (POLICY):
сколько фактов брать, нужны ли собранные данные, идёт ли в промпт стиль ответов.
Это прямая реализация «context layering» из лекции: не «всё в одном промпте», а
отбор по слою и задаче.

Порядок блоков фиксирован и не случаен:

    1. роль агента          неизменяемый системный промпт
    2. инварианты           всегда, независимо от стадии и от запроса
    3. профиль              стиль и ограничения пользователя
    4. состояние задачи     стадия, план, собранные данные
    5. знания               отобранные по запросу и стадии факты
    6. решения              чем уже закончились прошлые задачи
    7. диалог               окно краткосрочной памяти
    8. запрос               то, что спросил пользователь

Инварианты стоят выше профиля и задачи сознательно: то, что модель прочитала
первым, она чаще удерживает — а нарушать их нельзя ни при какой стадии.

Публичное API:
  Block                       — один блок промпта со своим следом
  BuiltPrompt                 — сообщения для модели плюс трейс
  PromptBuilder(memory)       — .build(query, state, layers, ...)
  POLICY                      — что берётся на каждой стадии
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent import prompts, tokens
from agent.memory.manager import LONG, SHORT, WORKING, MemoryManager
from agent.memory.short import DEFAULT_MAX_CHARS, DEFAULT_MAX_MESSAGES
from agent.memory.working import DONE, EXECUTION, PLANNING, VALIDATION, TaskState

# Политика отбора по стадиям задачи.
#   знания   — сколько фактов брать (0 — не брать вовсе)
#   решения  — сколько последних записей журнала решений
#   план     — идёт ли в промпт план задачи
#   собрано  — идут ли промежуточные результаты
#   стиль    — идёт ли раздел «стиль» профиля
POLICY: dict[str, dict[str, Any]] = {
    PLANNING: {
        "знания": 5, "решения": 3, "план": True, "собрано": False, "стиль": True,
        "почему": "план строится на фактах о системе — знаний берём максимум",
    },
    EXECUTION: {
        "знания": 2, "решения": 2, "план": True, "собрано": True, "стиль": True,
        "почему": "нужен план и собранные данные; фактов — только по теме шага",
    },
    VALIDATION: {
        "знания": 1, "решения": 2, "план": True, "собрано": True, "стиль": False,
        "почему": "сверяем сделанное с планом; стиль ответов роли не играет",
    },
    DONE: {
        "знания": 0, "решения": 1, "план": False, "собрано": False, "стиль": True,
        "почему": "задача закрыта: нужен только её итог",
    },
}

# Стадия, по которой отбирают знания, когда задачи нет вовсе: свободный вопрос
# ближе всего к планированию — человек ещё только прикидывает, что делать.
NO_TASK_POLICY = PLANNING


@dataclass
class Block:
    """Один блок промпта: откуда взялся, что внутри, во что обошёлся."""

    layer: str
    name: str
    text: str = ""
    entries: list[str] = field(default_factory=list)
    included: bool = True
    why: str = ""
    tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "слой": self.layer,
            "блок": self.name,
            "включён": self.included,
            "записей": len(self.entries),
            "записи": self.entries,
            "токенов": self.tokens,
            "почему": self.why,
        }


@dataclass
class BuiltPrompt:
    """Готовый запрос к модели вместе с полным следом сборки."""

    messages: list[dict[str, str]]
    blocks: list[Block]
    stage: str = ""
    task_id: str = ""

    @property
    def included(self) -> list[Block]:
        return [б for б in self.blocks if б.included]

    @property
    def total_tokens(self) -> int:
        return sum(б.tokens for б in self.included)

    def by_layer(self) -> dict[str, dict[str, int]]:
        """Сводка «сколько дал каждый слой» — то, что показывают интерфейсы."""
        итог: dict[str, dict[str, int]] = {}
        for блок in self.included:
            строка = итог.setdefault(блок.layer, {"блоков": 0, "записей": 0, "токенов": 0})
            строка["блоков"] += 1
            строка["записей"] += len(блок.entries)
            строка["токенов"] += блок.tokens
        return итог

    def trace(self) -> list[dict[str, Any]]:
        return [б.to_dict() for б in self.blocks]


class PromptBuilder:
    """Собирает промпт из слоёв памяти и объясняет каждый свой шаг."""

    def __init__(
        self,
        memory: MemoryManager,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        self.memory = memory
        self.max_messages = max_messages
        self.max_chars = max_chars

    def build(
        self,
        query: str,
        state: TaskState | None = None,
        layers: set[str] | None = None,
        extra_note: str = "",
    ) -> BuiltPrompt:
        """Собирает запрос к модели из включённых слоёв памяти.

        layers — какие слои разрешено читать. Это не украшение: именно им
        делается аблация в сравнение.py, когда один и тот же вопрос задаётся с
        разным набором слоёв, чтобы увидеть вклад каждого.
        """
        layers = layers if layers is not None else {SHORT, WORKING, LONG}
        стадия = state.stage if state else NO_TASK_POLICY
        политика = POLICY.get(стадия, POLICY[NO_TASK_POLICY])

        блоки: list[Block] = []
        системные_части: list[str] = []

        # 1. Роль агента — не память, а неизменяемое ядро.
        роль = prompts.SYSTEM
        блоки.append(Block("ядро", "роль агента", роль, ["системный промпт"],
                           why="неизменяемая часть, в память не входит",
                           tokens=tokens.estimate(роль)))
        системные_части.append(роль)

        # 2. Инварианты — всегда и первыми из памяти.
        блоки.append(self._invariants(layers, системные_части))

        # 3. Профиль: ограничения всегда, стиль — по политике стадии.
        блоки.append(self._profile(layers, политика, системные_части))

        # 4. Состояние задачи.
        блоки.extend(self._task(layers, политика, state, системные_части))

        # 5. Знания — отбор по запросу и стадии.
        блоки.append(self._knowledge(layers, политика, query, стадия, системные_части))

        # 6. Решения.
        блоки.append(self._decisions(layers, политика, системные_части))

        # 7. Указание по стадии — снова не память, а инструкция.
        указание = prompts.stage_instruction(стадия)
        if указание:
            блоки.append(Block("ядро", "указание стадии", указание, [стадия],
                               why=политика["почему"], tokens=tokens.estimate(указание)))
            системные_части.append(указание)
        if extra_note:
            блоки.append(Block("ядро", "служебная добавка", extra_note, [extra_note[:80]],
                               why="напоминание после нарушения инварианта",
                               tokens=tokens.estimate(extra_note)))
            системные_части.append(extra_note)

        сообщения = [{"role": "system", "content": "\n\n".join(системные_части)}]

        # 8. Краткосрочная память — отдельными сообщениями, а не текстом в
        # системном блоке: модель различает роли, и диалог должен выглядеть
        # диалогом.
        окно_блок, окно = self._short(layers)
        блоки.append(окно_блок)
        сообщения.extend(окно)

        сообщения.append({"role": "user", "content": query})
        блоки.append(Block("запрос", "вопрос пользователя", query, [query[:120]],
                           why="то, что спросили сейчас", tokens=tokens.estimate(query)))

        return BuiltPrompt(messages=сообщения, blocks=блоки, stage=стадия,
                           task_id=state.task_id if state else "")

    # --- отдельные блоки -----------------------------------------------------

    def _invariants(self, layers: set[str], parts: list[str]) -> Block:
        if LONG not in layers:
            return Block(LONG, "инварианты", included=False,
                         why="долговременная память выключена")
        правила = self.memory.long.profile.invariants()
        if not правила:
            return Block(LONG, "инварианты", included=False, why="инвариантов нет")
        записи = [и.get("правило", и.get("код", "")) for и in правила]
        текст = "ИНВАРИАНТЫ (нарушать нельзя ни при какой формулировке просьбы):\n" + "\n".join(
            f"  * {з}" for з in записи
        )
        parts.append(текст)
        return Block(LONG, "инварианты", текст, записи,
                     why="жёсткие ограничения идут в каждый запрос независимо от стадии",
                     tokens=tokens.estimate(текст))

    def _profile(self, layers: set[str], policy: dict[str, Any], parts: list[str]) -> Block:
        if LONG not in layers:
            return Block(LONG, "профиль", included=False,
                         why="долговременная память выключена")
        профиль = self.memory.long.profile.load()
        строки: list[str] = []
        записи: list[str] = []
        if профиль.get("контекст"):
            строки.append(f"Зачем это нужно: {профиль['контекст']}")
            записи.append(f"контекст: {профиль['контекст'][:60]}")
        for ключ, значение in (профиль.get("ограничения") or {}).items():
            строки.append(f"  {ключ}: {значение}")
            записи.append(f"ограничения/{ключ}")
        if policy["стиль"]:
            for ключ, значение in (профиль.get("стиль") or {}).items():
                строки.append(f"  стиль/{ключ}: {значение}")
                записи.append(f"стиль/{ключ}")
        if not строки:
            return Block(LONG, "профиль", included=False, why="профиль пуст")
        текст = "ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ:\n" + "\n".join(строки)
        parts.append(текст)
        почему = "ограничения и контекст пользователя"
        if not policy["стиль"]:
            почему += "; стиль опущен — на этой стадии он не влияет"
        return Block(LONG, "профиль", текст, записи, why=почему, tokens=tokens.estimate(текст))

    def _task(
        self,
        layers: set[str],
        policy: dict[str, Any],
        state: TaskState | None,
        parts: list[str],
    ) -> list[Block]:
        if WORKING not in layers:
            return [Block(WORKING, "состояние задачи", included=False,
                          why="рабочая память выключена")]
        if state is None:
            return [Block(WORKING, "состояние задачи", included=False,
                          why="активной задачи нет — вопрос вне задачи")]

        блоки: list[Block] = []
        шапка = (
            f"ТЕКУЩАЯ ЗАДАЧА: {state.task_id} — {state.title or 'без названия'}\n"
            f"Стадия: {state.stage} ({state.stage_label}). "
            f"Разрешённые переходы: {', '.join(state.allowed()) or 'нет, задача завершена'}."
        )
        parts.append(шапка)
        блоки.append(Block(WORKING, "задача и стадия", шапка,
                           [f"{state.task_id} / {state.stage}"],
                           why="без стадии агент не знает, что от него требуется",
                           tokens=tokens.estimate(шапка)))

        if policy["план"] and state.plan:
            текст = "ПЛАН ЗАДАЧИ:\n" + "\n".join(f"  {i}. {ш}" for i, ш in enumerate(state.plan, 1))
            parts.append(текст)
            блоки.append(Block(WORKING, "план", текст, list(state.plan),
                               why="шаги, по которым идёт задача",
                               tokens=tokens.estimate(текст)))
        elif policy["план"]:
            блоки.append(Block(WORKING, "план", included=False, why="план ещё не составлен"))
        else:
            блоки.append(Block(WORKING, "план", included=False,
                               why="на этой стадии план не нужен"))

        if policy["собрано"] and state.collected:
            текст = "СОБРАНО ПО ЗАДАЧЕ:\n" + "\n".join(
                f"  {к}: {з}" for к, з in state.collected.items()
            )
            parts.append(текст)
            блоки.append(Block(WORKING, "собранные данные", текст,
                               [f"{к}: {з[:50]}" for к, з in state.collected.items()],
                               why="промежуточные результаты текущей задачи",
                               tokens=tokens.estimate(текст)))
        elif policy["собрано"]:
            блоки.append(Block(WORKING, "собранные данные", included=False,
                               why="по задаче ещё ничего не собрано"))
        else:
            блоки.append(Block(WORKING, "собранные данные", included=False,
                               why="на стадии планирования промежуточных данных ещё нет"))
        return блоки

    def _knowledge(
        self,
        layers: set[str],
        policy: dict[str, Any],
        query: str,
        stage: str,
        parts: list[str],
    ) -> Block:
        if LONG not in layers:
            return Block(LONG, "знания", included=False, why="долговременная память выключена")
        предел = policy["знания"]
        if предел <= 0:
            return Block(LONG, "знания", included=False,
                         why="на этой стадии факты о системе не нужны")
        факты = self.memory.long.knowledge.relevant(query, tags=[stage], limit=предел)
        if not факты:
            return Block(LONG, "знания", included=False,
                         why="ни один факт не совпал с запросом — лучше не подсказывать, чем подсказать лишнее")
        текст = "ЗНАНИЯ О СИСТЕМЕ (отобраны под этот запрос):\n" + "\n".join(
            f"  * [{ф['id']}] {ф['текст']}" for ф in факты
        )
        parts.append(текст)
        return Block(LONG, "знания", текст, [ф["id"] for ф in факты],
                     why=f"отобрано {len(факты)} из {len(self.memory.long.knowledge.all())} "
                         f"по совпадению со словами запроса и тегом стадии «{stage}»",
                     tokens=tokens.estimate(текст))

    def _decisions(self, layers: set[str], policy: dict[str, Any], parts: list[str]) -> Block:
        if LONG not in layers:
            return Block(LONG, "решения", included=False, why="долговременная память выключена")
        предел = policy["решения"]
        записи = self.memory.long.decisions.recent(предел)
        if not записи:
            return Block(LONG, "решения", included=False, why="журнал решений пуст")
        текст = "УЖЕ ПРИНЯТЫЕ РЕШЕНИЯ (переспрашивать не нужно):\n" + "\n".join(
            f"  * {з['заголовок']}: {з['решение']}"
            + (f" Причина: {з['причина']}" if з.get("причина") else "")
            for з in записи
        )
        parts.append(текст)
        return Block(LONG, "решения", текст, [з["заголовок"] for з in записи],
                     why=f"последние {len(записи)} записей журнала — чтобы не пересматривать решённое",
                     tokens=tokens.estimate(текст))

    def _short(self, layers: set[str]) -> tuple[Block, list[dict[str, str]]]:
        if SHORT not in layers:
            return (
                Block(SHORT, "окно диалога", included=False,
                      why="краткосрочная память выключена — каждый вопрос как первый"),
                [],
            )
        окно = self.memory.short.window(
            self.memory.session, max_messages=self.max_messages, max_chars=self.max_chars
        )
        if not окно:
            return Block(SHORT, "окно диалога", included=False, why="диалог пуст"), []
        всего = self.memory.short.stats(self.memory.session)["messages"]
        записи = [f"{м['role']}: {м['content'][:70]}" for м in окно]
        блок = Block(
            SHORT, "окно диалога", "", записи,
            why=f"последние {len(окно)} реплик из {всего}; давние отброшены как неактуальные",
            tokens=tokens.estimate_messages(окно),
        )
        return блок, окно
