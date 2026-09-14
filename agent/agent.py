"""MemoryAgent — агент с явной моделью памяти.

Отличие этого дня от предыдущих в том, что у агента больше нет «истории» как
одной сущности. Есть три слоя с разным сроком жизни, разными хранилищами и
разными правилами записи, и каждый шаг агента проходит через них явно:

    вопрос
      -> маршрутизатор: нужно ли что-то сохранить надолго       (MemoryManager)
      -> запись реплики в краткосрочную память                  (MemoryManager)
      -> сборка промпта из слоёв по политике стадии             (PromptBuilder)
      -> вызов модели                                           (llm.Client)
      -> проверка ответа кодом по жёстким инвариантам           (StateValidator)
      -> при нарушении: повтор, затем эскалация модели
      -> запись ответа в краткосрочную память                   (MemoryManager)

Наружу агент отдаёт не только текст ответа, но и трейс: какие записи какого
слоя попали в промпт и во что это обошлось. Без трейса выполнить требование
задания «проверьте, какие данные попадают в каждый слой» нельзя — пришлось бы
верить на слово.

Публичное API:
  MemoryAgent(...)                  — создать агента
  .ask(question)                    — спросить с учётом всех включённых слоёв
  .plan()                           — составить план задачи и записать в рабочую память
  .start_task / .use_task / .transition / .finish_task
  .remember(target, ...)            — записать в указанный слой явно
  .set_layers(...)                  — включить и выключить слои (аблация)
  .info() / .stats() / .journal() / .files()
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from agent import catalog, seed as seed_module
from agent.builder import BuiltPrompt, PromptBuilder
from agent.llm import Client, LLMError, Reply
from agent.memory.manager import AUTO, LONG, SHORT, WORKING, MemoryManager
from agent.memory.router import DEFAULT_THRESHOLD, Routing
from agent.memory.short import DEFAULT_MAX_CHARS, DEFAULT_MAX_MESSAGES
from agent.memory.working import (
    DONE, PLANNING, TaskState, TransitionError, WorkingMemoryError,
)
from agent.validator import SoftResult, StateValidator, Violation

log = logging.getLogger("agent")

ALL_LAYERS = {SHORT, WORKING, LONG}

# Сколько раз агент пытается получить ответ, не нарушающий инварианты.
# Первая попытка — обычная; вторая — с напоминанием; третья — на модели
# следующей ступени. Дальше уже честнее отказать, чем жечь лимиты.
MAX_ATTEMPTS = 3

_ШАГ_ПЛАНА = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+(.{3,})$", re.MULTILINE)


class AgentError(RuntimeError):
    """Единственный тип ошибки, который агент выпускает наружу."""


@dataclass
class Answer:
    """Ответ агента вместе со всем, что понадобилось, чтобы его получить."""

    text: str
    prompt: BuiltPrompt | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    attempts: int = 1
    violations: list[Violation] = field(default_factory=list)
    blocked: bool = False            # ответ так и не уложился в инварианты
    escalated_to: str = ""
    routing: Routing | None = None
    routing_entry: dict[str, Any] = field(default_factory=dict)
    soft: SoftResult | None = None

    def layers(self) -> dict[str, dict[str, int]]:
        return self.prompt.by_layer() if self.prompt else {}

    def trace(self) -> list[dict[str, Any]]:
        return self.prompt.trace() if self.prompt else []

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "attempts": self.attempts,
            "blocked": self.blocked,
            "escalated_to": self.escalated_to,
            "usage": self.usage,
            "violations": [н.to_dict() for н in self.violations],
            "layers": self.layers(),
            "trace": self.trace(),
            "routing": self.routing.to_dict() if self.routing else None,
            "routing_entry": self.routing_entry,
            "soft": self.soft.to_dict() if self.soft else None,
            "stage": self.prompt.stage if self.prompt else "",
            "task_id": self.prompt.task_id if self.prompt else "",
        }


class MemoryAgent:
    """Агент, у которого память разложена по трём слоям явно."""

    def __init__(
        self,
        model_key: str = "",
        user_id: str = "инженер",
        session: str = "основная",
        base_dir: str = "",
        task_id: str = "",
        layers: set[str] | None = None,
        router_mode: str = AUTO,
        threshold: float = DEFAULT_THRESHOLD,
        router_model: str = "",
        max_messages: int = DEFAULT_MAX_MESSAGES,
        max_chars: int = DEFAULT_MAX_CHARS,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        soft_check: bool = False,
        seed_project: bool = True,
    ) -> None:
        # Пустой model_key означает «брать модель по роли»: на планировании и
        # на исполнении роли разные, и жёстко фиксировать одну модель не нужно.
        self.model_key = model_key
        self.layers = set(layers) if layers is not None else set(ALL_LAYERS)
        self.soft_check = soft_check

        self.client = Client(temperature=temperature, max_tokens=max_tokens)
        база = base_dir or os.getenv("MEMORY_DIR", "память")
        try:
            self.memory = MemoryManager(
                base_dir=база, user_id=user_id, session=session, client=self.client,
                router_mode=router_mode, threshold=threshold, router_model=router_model,
            )
        except (OSError, ValueError) as exc:
            raise AgentError(f"Не удалось открыть память: {exc}") from exc

        if seed_project:
            seed_module.seed(self.memory)

        self.builder = PromptBuilder(self.memory, max_messages=max_messages, max_chars=max_chars)
        self.validator = StateValidator(self.memory.long.profile, self.client)

        self.task: TaskState | None = None
        if task_id:
            self.use_task(task_id)

    # --- свойства ------------------------------------------------------------

    @property
    def session(self) -> str:
        return self.memory.session

    @property
    def user_id(self) -> str:
        return self.memory.user_id

    @property
    def stage(self) -> str:
        return self.task.stage if self.task else ""

    def model_for(self, stage: str = "") -> str:
        """Какая модель отвечает на этой стадии.

        Планирование и исполнение разведены по ролям: на планировании цена
        ошибки выше, потому что на плане строится всё остальное.
        """
        if self.model_key:
            return self.model_key
        роль = "планирование" if (stage or self.stage) == PLANNING else "исполнение"
        return catalog.for_role(роль, offset=0)

    # --- слои ----------------------------------------------------------------

    def set_layers(self, layers: set[str]) -> None:
        """Включает и выключает слои. Этим делается аблация в сравнение.py."""
        неизвестные = layers - ALL_LAYERS
        if неизвестные:
            raise AgentError(
                f"Неизвестные слои: {', '.join(неизвестные)}. "
                f"Допустимы: {', '.join(sorted(ALL_LAYERS))}."
            )
        self.layers = set(layers)

    # --- задачи --------------------------------------------------------------

    def start_task(self, task_id: str, title: str = "", overwrite: bool = False) -> TaskState:
        """Заводит задачу и делает её текущей."""
        try:
            self.task = self.memory.working.create(task_id, title, overwrite=overwrite)
        except WorkingMemoryError as exc:
            raise AgentError(str(exc)) from exc
        self.memory._log("шаг-задачи", WORKING, task_id, title, applied=True,
                         reason="задача заведена, стадия planning")
        return self.task

    def use_task(self, task_id: str) -> TaskState:
        """Поднимает задачу из рабочей памяти — в том числе спустя дни."""
        try:
            self.task = self.memory.working.load(task_id)
        except WorkingMemoryError as exc:
            raise AgentError(str(exc)) from exc
        return self.task

    def drop_task(self) -> None:
        self.task = None

    def transition(self, stage: str, note: str = "") -> TaskState:
        """Переводит задачу на новую стадию; запрещённый переход — ошибка."""
        if self.task is None:
            raise AgentError("Активной задачи нет: сначала --задача или --новая-задача.")
        try:
            self.task.transition(stage, note)
        except TransitionError as exc:
            raise AgentError(str(exc)) from exc
        self.memory.working.save(self.task)
        self.memory._log("шаг-задачи", WORKING, self.task.task_id, f"-> {stage}", applied=True,
                         reason=note or "смена стадии")
        return self.task

    def finish_task(self, note: str = "") -> dict[str, Any]:
        """Завершает задачу: свёртка в журнал решений и очистка рабочей памяти."""
        if self.task is None:
            raise AgentError("Активной задачи нет.")
        if self.task.stage != DONE and not self.task.can_go(DONE):
            raise AgentError(
                f"Из стадии «{self.task.stage}» нельзя сразу в done. "
                f"Разрешено: {', '.join(self.task.allowed())}."
            )
        запись = self.memory.finish_task(self.task, note)
        self.task = None
        return запись

    # --- запись в память -----------------------------------------------------

    def remember(self, target: str, value: str, key: str = "", section: str = "",
                 reason: str = "") -> dict[str, Any]:
        """Явная запись в указанный слой. Модель не участвует."""
        try:
            return self.memory.remember_explicit(
                target, value, key=key, section=section, reason=reason,
                task_id=self.task.task_id if self.task else "",
            )
        except Exception as exc:  # LongTermError, WorkingMemoryError
            raise AgentError(str(exc)) from exc

    def remember_step(self, key: str, value: str) -> TaskState:
        if self.task is None:
            raise AgentError("Активной задачи нет: промежуточный результат некуда класть.")
        self.task = self.memory.remember_step(self.task, key, value)
        return self.task

    # --- основной цикл -------------------------------------------------------

    def ask(self, question: str, layers: set[str] | None = None) -> Answer:
        """Полный цикл: маршрутизация, сборка, вызов, проверка, запись."""
        question = (question or "").strip()
        if not question:
            raise AgentError("Пустой вопрос.")
        слои = set(layers) if layers is not None else set(self.layers)

        # Правило 5: может быть, в реплике есть что-то для долговременной памяти.
        # Если долговременный слой выключен, спрашивать маршрутизатор незачем.
        маршрут: Routing | None = None
        запись_маршрута: dict[str, Any] = {}
        if LONG in слои:
            маршрут, запись_маршрута = self.memory.route(question)

        # Правило 2: сама реплика — в краткосрочную память.
        if SHORT in слои:
            self.memory.remember_message(
                "user", question,
                task_id=self.task.task_id if self.task else "",
                stage=self.stage,
            )

        промпт = self.builder.build(question, self.task, слои)
        ответ, попытки, нарушения, эскалация = self._answer_within_invariants(промпт, question, слои)

        мягкая: SoftResult | None = None
        if self.soft_check and not нарушения:
            мягкая = self.validator.check_soft(ответ.text)

        if SHORT in слои:
            self.memory.remember_message(
                "assistant", ответ.text,
                task_id=self.task.task_id if self.task else "",
                stage=self.stage,
                tokens=ответ.total_tokens, cost=ответ.cost,
            )

        return Answer(
            text=ответ.text,
            prompt=промпт,
            usage=ответ.to_dict(),
            attempts=попытки,
            violations=нарушения,
            blocked=bool(нарушения),
            escalated_to=эскалация,
            routing=маршрут,
            routing_entry=запись_маршрута,
            soft=мягкая,
        )

    def _answer_within_invariants(
        self, prompt: BuiltPrompt, question: str, layers: set[str],
    ) -> tuple[Reply, int, list[Violation], str]:
        """Получает ответ, не нарушающий жёстких инвариантов.

        Лестница ровно та, что описана в лекции про ретраи: сначала попросить
        переделать ту же модель, и только если это не помогло — менять модель.
        Прыгать сразу на самую дорогую незачем: чаще всего хватает напоминания.
        """
        ключ_модели = self.model_for(prompt.stage)
        сообщения = prompt.messages
        эскалация = ""
        нарушения: list[Violation] = []

        for попытка in range(1, MAX_ATTEMPTS + 1):
            try:
                ответ = self.client.call(ключ_модели, сообщения)
            except LLMError as exc:
                raise AgentError(str(exc)) from exc

            нарушения = self.validator.check(ответ.text) if LONG in layers else []
            if not нарушения:
                return ответ, попытка, [], эскалация

            log.warning("Попытка %d нарушила инварианты: %s", попытка,
                        "; ".join(str(н) for н in нарушения))
            if попытка == MAX_ATTEMPTS:
                break

            напоминание = self.validator.reminder(нарушения)
            повтор = self.builder.build(question, self.task, layers, extra_note=напоминание)
            сообщения = повтор.messages
            if попытка >= 2:
                # Напоминание не помогло — поднимаемся на одну ступень лестницы.
                следующая = catalog.escalate(ключ_модели)
                if следующая != ключ_модели:
                    ключ_модели, эскалация = следующая, следующая

        return ответ, MAX_ATTEMPTS, нарушения, эскалация

    # --- планирование --------------------------------------------------------

    def plan(self, note: str = "") -> Answer:
        """Просит модель составить план и кладёт его в рабочую память.

        Это единственное место, где ответ модели превращается в структуру, а не
        остаётся текстом: план — рабочие данные задачи, и жить он должен в
        рабочей памяти, а не в переписке.
        """
        if self.task is None:
            raise AgentError("Активной задачи нет: планировать нечего.")
        if self.task.stage != PLANNING:
            raise AgentError(
                f"План составляется на стадии planning, а задача сейчас в «{self.task.stage}»."
            )
        вопрос = (
            f"Составь план задачи «{self.task.title or self.task.task_id}». "
            + (note or "")
            + " Дай нумерованный список шагов, по одному шагу в строке, без пояснений между ними."
        )
        ответ = self.ask(вопрос)
        шаги = [ш.strip() for ш in _ШАГ_ПЛАНА.findall(ответ.text)][:12]
        if шаги:
            self.task = self.memory.remember_plan(self.task, шаги)
        return ответ

    # --- сводка --------------------------------------------------------------

    def info(self) -> dict[str, Any]:
        """Состояние агента для интерфейсов."""
        модель = self.model_for()
        описание = catalog.get(модель)
        return {
            "model_key": модель,
            "model_label": описание.label,
            "model_fixed": bool(self.model_key),
            "provider": описание.provider,
            "free": описание.free,
            "user_id": self.user_id,
            "session": self.session,
            "layers": sorted(self.layers),
            "router_mode": self.memory.router_mode,
            "threshold": self.memory.threshold,
            "task": self.task.to_dict() if self.task else None,
            "stage": self.stage,
            "allowed": list(self.task.allowed()) if self.task else [],
            "soft_check": self.soft_check,
            "spent": dict(self.client.spent),
        }

    def stats(self) -> dict[str, Any]:
        return self.memory.stats()

    def journal(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.memory.journal(limit)

    def files(self) -> dict[str, str]:
        return self.memory.files()

    def tasks(self) -> list[dict[str, Any]]:
        return self.memory.working.tasks()

    def close(self) -> None:
        self.client.close()
