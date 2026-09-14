"""Рабочая память: состояние текущей задачи.

Между «что мы только что сказали» и «что мы знаем про проект вообще» есть
третье — то, чем агент занят прямо сейчас: какая задача решается, на каком она
этапе, каков план и что уже собрано по ходу дела. Этот слой живёт от начала
задачи до её завершения: дольше отдельной реплики, но короче, чем знания о
проекте.

Устройство слоя — конечный автомат из четырёх стадий:

    planning -> execution -> validation -> done

Переходы разрешены не любые, и список разрешённых задан явно (TRANSITIONS).
Это единственный способ добавить детерминированности недетерминированной
модели: модель может сколько угодно «считать», что задача готова, но пока код
не увидел разрешённого перехода validation -> done, задача не завершена.

Хранится состояние отдельным JSON-файлом на задачу, а не в базе диалога.
Причина прикладная: задачу надо уметь поднять через неделю после того, как
диалог, в котором она началась, был стёрт. Файл переживает и перезапуск, и
очистку истории, а прочитать его можно обычным cat.

Публичное API:
  STAGES, TRANSITIONS               — стадии и разрешённые переходы
  TaskState                         — состояние одной задачи
  WorkingMemory(directory)          — хранилище задач
  .create(task_id, title, ...)      — завести задачу
  .load(task_id) / .save(state)     — прочитать и записать
  .tasks()                          — список задач со сводкой
  .drop(task_id)                    — убрать задачу из рабочей памяти
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

PLANNING = "planning"
EXECUTION = "execution"
VALIDATION = "validation"
DONE = "done"

# Базовые стадии лекции убирать нельзя — детализировать можно, но эти четыре
# остаются костяком.
STAGES = (PLANNING, EXECUTION, VALIDATION, DONE)

STAGE_LABELS = {
    PLANNING: "планирование",
    EXECUTION: "исполнение",
    VALIDATION: "проверка",
    DONE: "готово",
}

# Разрешённые переходы. Обратные стрелки не украшение: из исполнения можно
# вернуться в планирование (план оказался негодным), из проверки — в исполнение
# (нашли дефект). А вот перепрыгнуть из планирования сразу в готово нельзя.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    PLANNING: (EXECUTION,),
    EXECUTION: (VALIDATION, PLANNING),
    VALIDATION: (EXECUTION, DONE),
    DONE: (),
}

_ID = re.compile(r"^[a-zA-Zа-яёА-ЯЁ0-9][a-zA-Zа-яёА-ЯЁ0-9_\-]{0,63}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WorkingMemoryError(RuntimeError):
    """Ошибка работы с рабочей памятью."""


class TransitionError(WorkingMemoryError):
    """Запрошен переход, которого нет в списке разрешённых."""


@dataclass
class TaskState:
    """Состояние одной задачи: этап, план, собранные данные, история переходов."""

    task_id: str
    title: str = ""
    stage: str = PLANNING
    plan: list[str] = field(default_factory=list)
    # Промежуточные результаты: то, что выяснили по ходу задачи. Ключ —
    # о чём речь, значение — сам результат. Живёт ровно столько, сколько задача.
    collected: dict[str, str] = field(default_factory=dict)
    # Журнал переходов: кто, когда и почему сменил стадию. Нужен на разборе
    # полётов — по нему видно, сколько раз задача возвращалась на доработку.
    transitions: list[dict[str, str]] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @property
    def stage_label(self) -> str:
        return STAGE_LABELS.get(self.stage, self.stage)

    @property
    def finished(self) -> bool:
        return self.stage == DONE

    def allowed(self) -> tuple[str, ...]:
        """Куда из текущей стадии можно перейти."""
        return TRANSITIONS.get(self.stage, ())

    def can_go(self, stage: str) -> bool:
        return stage in self.allowed()

    def transition(self, stage: str, note: str = "") -> None:
        """Переводит задачу на новую стадию; запрещённый переход — ошибка.

        Именно здесь недетерминированность модели упирается в код: текст ответа
        может сколько угодно утверждать, что «всё готово», — стадию меняет
        только явный разрешённый переход.
        """
        if stage not in STAGES:
            raise TransitionError(
                f"Неизвестная стадия «{stage}». Допустимы: {', '.join(STAGES)}."
            )
        if not self.can_go(stage):
            разрешено = ", ".join(self.allowed()) or "никуда: задача завершена"
            raise TransitionError(
                f"Переход {self.stage} -> {stage} не разрешён. "
                f"Из стадии «{self.stage}» можно: {разрешено}."
            )
        self.transitions.append(
            {"from": self.stage, "to": stage, "note": note, "at": _now()}
        )
        self.stage = stage
        self.updated_at = _now()

    def remember(self, key: str, value: str) -> None:
        """Кладёт промежуточный результат в собранные данные задачи."""
        key = (key or "").strip()
        if not key:
            raise WorkingMemoryError("У промежуточного результата должен быть ключ.")
        self.collected[key] = (value or "").strip()
        self.updated_at = _now()

    def set_plan(self, steps: list[str]) -> None:
        self.plan = [ш.strip() for ш in steps if ш.strip()]
        self.updated_at = _now()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskState":
        известные = {поле for поле in cls.__dataclass_fields__}
        return cls(**{к: з for к, з in data.items() if к in известные})

    def digest(self) -> str:
        """Короткая сводка задачи одной строкой — для списков и логов."""
        шагов = len(self.plan)
        данных = len(self.collected)
        return (
            f"{self.task_id} [{self.stage_label}] {self.title or '—'}; "
            f"шагов плана {шагов}, собрано записей {данных}"
        )


class WorkingMemory:
    """Хранилище задач: по JSON-файлу на задачу в отдельном каталоге."""

    layer = "рабочая"

    def __init__(self, directory: str) -> None:
        self.directory = directory
        os.makedirs(directory, exist_ok=True)

    def _path(self, task_id: str) -> str:
        if not _ID.match(task_id or ""):
            raise WorkingMemoryError(
                f"Недопустимый идентификатор задачи «{task_id}»: нужны буквы, цифры, "
                "дефис и подчёркивание, до 64 символов."
            )
        return os.path.join(self.directory, f"{task_id}.json")

    def exists(self, task_id: str) -> bool:
        return os.path.exists(self._path(task_id))

    def create(
        self,
        task_id: str,
        title: str = "",
        plan: list[str] | None = None,
        overwrite: bool = False,
    ) -> TaskState:
        """Заводит новую задачу в стадии planning."""
        if self.exists(task_id) and not overwrite:
            raise WorkingMemoryError(
                f"Задача «{task_id}» уже есть в рабочей памяти. "
                "Возьмите её (--задача) или укажите другой идентификатор."
            )
        состояние = TaskState(task_id=task_id, title=title, plan=plan or [])
        self.save(состояние)
        return состояние

    def load(self, task_id: str) -> TaskState:
        """Поднимает состояние задачи с диска — в том числе спустя дни."""
        путь = self._path(task_id)
        try:
            with open(путь, encoding="utf-8") as файл:
                данные = json.load(файл)
        except FileNotFoundError:
            raise WorkingMemoryError(
                f"Задачи «{task_id}» нет в рабочей памяти. Список: --задачи."
            ) from None
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkingMemoryError(f"Файл задачи «{task_id}» испорчен: {exc}") from exc
        return TaskState.from_dict(данные)

    def save(self, state: TaskState) -> None:
        """Записывает состояние задачи атомарно: сначала временный файл, потом замена."""
        state.updated_at = _now()
        путь = self._path(state.task_id)
        временный = путь + ".tmp"
        try:
            with open(временный, "w", encoding="utf-8") as файл:
                json.dump(state.to_dict(), файл, ensure_ascii=False, indent=2)
            os.replace(временный, путь)
        except OSError as exc:
            raise WorkingMemoryError(f"Не удалось сохранить задачу «{state.task_id}»: {exc}") from exc

    def tasks(self) -> list[dict[str, Any]]:
        """Все задачи рабочей памяти со сводкой, свежие сверху."""
        итог = []
        for имя in sorted(os.listdir(self.directory)):
            if not имя.endswith(".json"):
                continue
            try:
                состояние = self.load(имя[:-5])
            except WorkingMemoryError:
                continue
            итог.append(
                {
                    "task_id": состояние.task_id,
                    "title": состояние.title,
                    "stage": состояние.stage,
                    "stage_label": состояние.stage_label,
                    "plan_steps": len(состояние.plan),
                    "collected": len(состояние.collected),
                    "updated_at": состояние.updated_at,
                }
            )
        итог.sort(key=lambda з: з["updated_at"], reverse=True)
        return итог

    def drop(self, task_id: str) -> bool:
        """Убирает задачу из рабочей памяти.

        Вызывается после того, как итог задачи свёрнут в журнал решений: рабочая
        память — не архив, и держать в ней завершённое незачем.
        """
        try:
            os.remove(self._path(task_id))
            return True
        except FileNotFoundError:
            return False
