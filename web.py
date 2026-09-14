#!/usr/bin/env python3
"""Веб-интерфейс: чат с агентом и наглядная раскладка памяти по слоям.

Запуск:
    python web.py
Затем открыть http://127.0.0.1:5000

Главная мысль страницы — не чат, а три колонки справа от него: краткосрочная,
рабочая и долговременная память со своим содержимым. После каждого ответа
показывается разбор промпта: какой блок какого слоя туда вошёл, сколько занял
и почему был включён или пропущен.

Как и cli.py, этот файл — только интерфейс: он вызывает публичные методы агента
и рисует результат.
"""

from __future__ import annotations

import logging

from flask import Flask, jsonify, render_template, request

from agent import AgentError, MemoryAgent
from agent import catalog
from agent.memory.manager import ASK, AUTO, LONG, OFF, SHORT, WORKING
from agent.memory.working import STAGES

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

app = Flask(__name__)
agent = MemoryAgent()

# Имена слоёв в том виде, в каком их присылает страница.
ИМЕНА_СЛОЁВ = {"кратко": SHORT, "рабочая": WORKING, "долго": LONG}


def _применить(данные: dict) -> None:
    """Приводит агента в состояние, выбранное на странице.

    Страница живёт дольше запроса, поэтому набор слоёв, режим маршрутизатора и
    текущая задача присылаются с каждым обращением: так после перезапуска
    сервера страница не окажется рассинхронизирована с агентом.
    """
    слои = данные.get("layers")
    if isinstance(слои, list):
        agent.set_layers({ИМЕНА_СЛОЁВ[с] for с in слои if с in ИМЕНА_СЛОЁВ})

    режим = (данные.get("router") or "").strip()
    if режим in (AUTO, ASK, OFF):
        agent.memory.router_mode = режим

    модель = (данные.get("model") or "").strip()
    agent.model_key = модель if модель in catalog.MODELS else ""

    задача = (данные.get("task") or "").strip()
    if задача and (agent.task is None or agent.task.task_id != задача):
        agent.use_task(задача)
    elif not задача and agent.task is not None:
        agent.drop_task()


def _состояние() -> dict:
    """Всё, что странице нужно, чтобы нарисовать текущее положение дел."""
    return {
        "info": agent.info(),
        "stats": agent.stats(),
        "files": agent.files(),
        "tasks": agent.tasks(),
        "profile": agent.memory.long.profile.load(),
        "knowledge": agent.memory.long.knowledge.all(),
        "decisions": agent.memory.long.decisions.all()[-10:],
        "dialog": agent.memory.short.all(agent.session)[-20:],
        "journal": agent.journal(20),
        "stages": list(STAGES),
    }


@app.route("/", methods=["GET"])
def index():
    return render_template(
        "index.html",
        info=agent.info(),
        models=catalog.describe(),
        roles=catalog.describe_roles(),
        stages=list(STAGES),
    )


@app.get("/api/state")
def api_state():
    """Состояние всех слоёв — этим страница и оживает при открытии."""
    return jsonify(_состояние())


@app.post("/api/ask")
def api_ask():
    данные = request.get_json(silent=True) or {}
    вопрос = (данные.get("question") or "").strip()
    if not вопрос:
        return jsonify({"error": "Пустой вопрос."}), 400
    try:
        _применить(данные)
        ответ = agent.ask(вопрос)
    except AgentError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"answer": ответ.to_dict(), "state": _состояние()})


@app.post("/api/plan")
def api_plan():
    данные = request.get_json(silent=True) or {}
    try:
        _применить(данные)
        ответ = agent.plan()
    except AgentError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"answer": ответ.to_dict(), "state": _состояние()})


@app.post("/api/task")
def api_task():
    """Заводит задачу, берёт существующую или переводит её на другую стадию."""
    данные = request.get_json(silent=True) or {}
    действие = (данные.get("action") or "").strip()
    try:
        if действие == "создать":
            agent.start_task((данные.get("task_id") or "").strip(),
                             (данные.get("title") or "").strip())
        elif действие == "взять":
            agent.use_task((данные.get("task_id") or "").strip())
        elif действие == "стадия":
            agent.transition((данные.get("stage") or "").strip(),
                             (данные.get("note") or "").strip())
        elif действие == "шаг":
            agent.remember_step((данные.get("key") or "").strip(),
                                (данные.get("value") or "").strip())
        elif действие == "завершить":
            запись = agent.finish_task((данные.get("note") or "").strip())
            return jsonify({"decision": запись, "state": _состояние()})
        elif действие == "отпустить":
            agent.drop_task()
        else:
            return jsonify({"error": f"Неизвестное действие «{действие}»."}), 400
    except AgentError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"state": _состояние()})


@app.post("/api/remember")
def api_remember():
    """Явная запись в выбранный слой — та самая «ручная» маршрутизация."""
    данные = request.get_json(silent=True) or {}
    слой = (данные.get("target") or "").strip()
    текст = (данные.get("value") or "").strip()
    if not текст:
        return jsonify({"error": "Нечего запоминать."}), 400
    try:
        запись = agent.remember(слой, текст,
                                key=(данные.get("key") or "").strip(),
                                section=(данные.get("section") or "").strip())
    except AgentError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"entry": запись, "state": _состояние()})


@app.post("/api/forget")
def api_forget():
    """Очищает краткосрочную память. Остальные слои не трогает — это разные слои."""
    стёрто = agent.memory.short.clear(agent.session)
    return jsonify({"cleared": стёрто, "state": _состояние()})


@app.get("/api/health")
def api_health():
    инфо = agent.info()
    return jsonify({"ok": True, "model": инфо["model_key"], "layers": инфо["layers"]})


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
