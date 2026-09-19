# Результаты прогона

Живой прогон на реальных ключах, 15 сентября 2026 года. Ответы приведены как
есть, без правки. Воспроизводится командами:

```
python compare.py --в RESULTS.md              # проверки 1-4
python compare.py --только-модели --платные   # проверка 5
```

Проверки 1-4 задают один и тот же вопрос дважды, включив и выключив ровно один
слой памяти. Признаки в таблицах считаются механически поиском по ответу: это
факт «слово встретилось», а не впечатление «ответ стал лучше».

## Коротко

| Проверка | Что выключали | Что изменилось в ответе |
|---|---|---|
| 1. Профиль | долговременную память | без неё ответ общий и допускает MySQL; с ней — таблица соответствия legacy-компонентам и список недостающих сведений |
| 2. Рабочая память | рабочую память | без неё «продолжай» начинает новый план вместо шага 2 существующего |
| 3. Диалог | краткосрочную память | без неё агент гадает имя таблицы («скорее всего `users` или `ci_user`») вместо той, о которой шла речь реплику назад |
| 4. Инварианты | долговременную память | без неё агент проектирует параллельный слой на Laravel и предлагает выбрать MySQL |
| 5. Модели | — | allam-2-7b ошибся на классификации; платные модели не дали преимущества на коде для PostGIS |


## 1. ПРОФИЛЬ: влияет ли долговременная память на выбор технологии

Вопрос поставлен так, что без профиля модель вольна предложить что угодно.

**Вопрос:** Нужно отдавать слои сети в веб-карту и редактировать геометрию из браузера. Какой backend и какую базу взять?

| признак в ответе | с долговременной | без неё |
|---|---|---|
| GeoDjango | да | да |
| Django | да | да |
| PostGIS | да | да |
| legacy-привязка | да | да |
| чужой стек | — | да |
| просит уточнить | да | — |
| токенов в запросе | 1262 | 348 |

### [с долговременной] слои: долговременная, краткосрочная, рабочая

```
**Backend** – Django 5.x + GeoDjango (django.contrib.gis).  
*Соответствует* старому контроллеру **CodeIgniter (userpgplace.php, openlayers.php, mapserver.php)** – отвечает за формирование ответов, аутентификацию и работу с геометрией, но реализован в едином Python‑фреймворке без сторонних библиотек.

**База данных** – PostgreSQL 17 + расширение PostGIS 3.6.  
*Соответствует* текущей базе **PostgreSQL 9.4 + PostGIS 2.1**, но используется современная версия, поддерживает типы геометрий, пространственные индексы и транзакции, необходимые для WFS‑T и редактирования из браузера.

**Кратко, почему именно эти компоненты**

| Требование | Legacy‑компонент | Новый компонент | Причина |
|------------|------------------|----------------|---------|
| Формирование API‑эндпоинтов, авторизация, бизнес‑логика | CodeIgniter‑контроллеры (userpgplace.php, auth.php) | Django + GeoDjango (REST‑view/DRF, JWT/OAuth2) | Полный стек Python, поддержка токен‑авторизации, удобная маршрутизация |
| Хранение и обработка геометрий | PostgreSQL 9.4 + PostGIS 2.1 | PostgreSQL 17 + PostGIS 3.6 | Современная СУБД, улучшенная производительность, совместимость с GeoDjango |
| Внешняя совместимость WMS/WFS | MapServer 6.4 (WMS/WFS) | GeoServer (WMS/WFS‑T) + Martin (векторные тайлы) | Сохраняет OGC‑интерфейсы, добавляет векторные тайлы |
| Клиентская карта | OpenLayers 2.13 | OpenLayers 10.x | Поддержка WFS‑T, snap‑инструментов, современный API |

**Что ещё нужно уточнить для полного проекта**

1. **Схема таблиц** (названия, типы полей, связи) – требуется для создания моделей GeoDjango.  
2. **Точки входа 1С** – какие параметры передаются в методы `ЗаписатьОбъектВГИС`, `УдалитьОбъектИзГИС`, `ПолнаяСинхронизация`.  
3. **Требования к правам доступа** – роли диспетчеров, внешних потребителей, мобильных клиент
```

### [без неё] слои: краткосрочная, рабочая

```
**Выбор backend‑а и СУБД**

| Что требуется в старой системе | Современный эквивалент |
|--------------------------------|------------------------|
| PHP‑скрипты (CodeIgniter) → API‑слой | **Django + Django REST Framework** (DRF) – REST‑API, аутентификация, валидация запросов |
| MapServer (WMS/WFS) → выдача растровых/векторных слоёв | **GeoDjango + Django‑GIS** + **PostGIS** – формирует GeoJSON, MVT (Mapbox Vector Tiles) и WFS‑подобные ответы |
| PostgreSQL + PostGIS (не использовалась) | **PostgreSQL + PostGIS** – единственная надёжная гео‑БД для векторных данных, поддерживает транзакции, версии, триггеры и индексы |
| OpenLayers 2 (клиент) → интерактивная карта | **OpenLayers 10** (подключается к REST‑/MVT‑эндпоинтам) |

**Рекомендованный стек**

- **Backend:** Django 3/4 + Django REST Framework + GeoDjango.  
  - Обрабатывает CRUD‑операции над объектами сети (пользовательские запросы → сериализаторы → модели).  
  - Предоставляет отдельные эндпоинты:  
    - `/api/pipelines/` – список/фильтрация, возвращает GeoJSON.  
    - `/api/pipelines/<id>/` – детали, PATCH/PUT для редактирования геометрии.  
    - `/tiles/{z}/{x}/{y}.mvt` – векторные тайлы (через `django‑vectortiles` или `django‑mapbox‑tiles`).  
- **База данных:** PostgreSQL 13+ + PostGIS 3+.  
  - Таблицы: `pipeline` (id, name, geom = LineString, attributes), `segment`, `node` и т.п.  
  - Индексы: GiST на `geom`, B‑Tree на часто фильтруемые атрибуты.  
  - При необходимости – включить **PostgreSQL‑based versioning** (например, `pg_partman` + триггер‑таблицы) для истории правок.

---

### План миграции (3‑7 проверяемых шагов)

1. **Анализ текущих данных**  
   - Выгрузить схему и содержимое всех таблиц, где хранятся геометрии (возможно, в файлах *.shp, *.sql, MySQL, SQLite).  
   - Сопоставить их с объектами
```

## 2. РАБОЧАЯ ПАМЯТЬ: помнит ли агент, на каком он шаге

Задача в стадии execution: план из 4 шагов, первый шаг выполнен.

**Вопрос:** Продолжай. Делай следующий шаг.

| признак в ответе | с рабочей памятью | без неё |
|---|---|---|
| GeoDjango | — | да |
| Django | да | да |
| PostGIS | — | да |
| legacy-привязка | да | да |
| чужой стек | — | — |
| просит уточнить | — | — |
| токенов в запросе | 1153 | 1017 |

### [с рабочей памятью] слои: долговременная, краткосрочная, рабочая

```
**Шаг 2 – модели Django для `account`, `group`, `organization`**  
(соответствует legacy‑таблицам **account**, **group**, **organization**)

```python
# core/models.py
from __future__ import annotations

from django.db import models
from django.contrib.auth.models import (
    AbstractBaseUser,
    PermissionsMixin,
    BaseUserManager,
)
from django.utils import timezone


# ---------- 1. Менеджер пользователя (соответствует legacy‑account) ----------
class AccountManager(BaseUserManager):
    def _create_user(self, username: str, password: str | None, **extra_fields):
        if not username:
            raise ValueError('The given username must be set')
        username = self.model.normalize_username(username)
        user = self.model(username=username, **extra_fields)
        user.set_password(password)          # хранит хеш, совместимо с PHP‑password_hash
        user.save(using=self._db)
        return user

    def create_user(self, username: str, password: str | None = None, **extra_fields):
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        return self._create_user(username, password, **extra_fields)

    def create_superuser(self, username: str, password: str, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')

        return self._create_user(username, password, **extra_fields)


# ---------- 2. Таблица account ----------
class Account(AbstractBaseUser, PermissionsMixin):
    """
```

### [без неё] слои: долговременная, краткосрочная

```
**Шаг 5 – Создание Django‑приложения `network`, описание моделей, админ‑интерфейса и утилиты импорта данных из старой БД.**  
*Соответствие legacy‑компонентам*  

| Legacy‑компонент | Что делаем в новом стеке |
|------------------|--------------------------|
| **MapServer‑слои** (каждый слой – отдельный слой OpenLayers) | Модели `Pipe`, `Equipment`, `Point`, `Valve` → таблицы в PostgreSQL + GeoServer будет читать их напрямую (WMS/WFS). |
| **OpenLayers 2 + MapServer** (редактирование геометрии) | Новый API‑эндпоинт (Django + DRF) + OpenLayers 10 → веб‑клиент будет посылать GeoJSON‑патчи, а модели сохранят геометрию. |
| **RedBean ORM** (SQL‑запросы к `pipes`, `equipment` и т.п.) | Django ORM + GeoDjango – полностью заменяет RedBean, но сохраняем имена таблиц/полей. |
| **PostgreSQL 9.4 + PostGIS 2.1** (источник данных) | В Docker‑compose добавляем отдельный контейнер `legacy_db` и мигрируем данные в `new_db` (PostgreSQL 17 + PostGIS 3.6). |

---

## 1. Docker‑compose (добавляем сервисы legacy‑DB и new‑DB)

```yaml
# docker-compose.yml (фрагмент)
version: "3.9"

services:
  legacy_db:
    image: postgres:9.4
    container_name: legacy_db
    environment:
      POSTGRES_DB: gis_legacy
      POSTGRES_USER: legacy_user
      POSTGRES_PASSWORD: legacy_pass
    volumes:
      - legacy_data:/var/lib/postgresql/data
    networks:
      - gisnet

  new_db:
    image: postgis/postgis:17-3.6
```

## 3. КРАТКОСРОЧНАЯ ПАМЯТЬ: понятно ли, к чему относится «туда»

Перед этим был задан вопрос: «Опиши модель Django для таблицы gissys.account: поля id, login, password_hash, fio, organization_id.»

**Вопрос:** Добавь туда поле last_login и индекс по нему.

| признак в ответе | с окном диалога | без него |
|---|---|---|
| GeoDjango | да | — |
| Django | да | да |
| PostGIS | да | да |
| legacy-привязка | да | да |
| чужой стек | — | — |
| просит уточнить | — | да |
| токенов в запросе | 1993 | 1209 |

### [с окном диалога] слои: долговременная, краткосрочная, рабочая

```
**Соответствие legacy‑компоненту**  
Таблица `gissys.account` — хранилище учётных записей, использовалась в CodeIgniter (RedBean ORM) для аутентификации. Поле `last_login` в legacy‑версии отсутствовало, но его добавление полностью совместимо с текущей бизнес‑логикой (отслеживание даты последнего входа) и не нарушает внешних зависимостей (WMS/WFS, 1С).

---

## Обновлённое описание модели Django (GeoDjango)

| Поле (legacy) | Django‑поле | Тип данных в PostgreSQL | Примечание |
|---------------|-------------|------------------------|------------|
| `id` | `AutoField(primary_key=True)` | `serial` / `bigserial` | Автогенерируемый PK. |
| `login` | `CharField(max_length=150, unique=True)` | `varchar(150)` | Уникальный логин. |
| `password_hash` | `CharField(max_length=255)` | `varchar(255)` | Хеш пароля. |
| `fio` | `CharField(max_length=255)` | `varchar(255)` | ФИО пользователя. |
| `organization_id` | `ForeignKey('Organization', on_delete=models.PROTECT, db_column='organization_id')` | `integer` (FK) | Ссылка на `gissys.organization`. |
| **`last_login`** | `DateTimeField(null=True, blank=True)` | `timestamp with time zone` | Дата/время последнего успешного входа. `null` — для пользователей, которые ещё не входили. |
| **Индекс** | `Meta.indexes = [models.Index(fields=['last_login'], name='idx_account_last_login')]` | `btree` | Ускоряет запросы сортировки/фильтрации по `last_login` (например, “показать активных за последние 30 дней”). |

**Meta‑опции модели**

```python
class Meta:
    db_table = 'gissys.account'          # сохраняет имя legacy‑таблицы
    ordering = ['login']
    indexes = [
        models.Index(fields=['last_login'], name='idx_account_last_login')
    ]
```

---

## План внедрения поля `last_login` и индекса (проверяемые шаги)

| Шаг | Действие | Как пр
```

### [без него] слои: долговременная, рабочая

```
**План миграции — добавление поля `last_login` и индекса по нему**  

| Шаг | Что делаем | Как это соотносится с legacy‑компонентом |
|-----|------------|------------------------------------------|
| 1️⃣ | **Анализ текущей модели пользователей** – в CodeIgniter‑контроллере `auth.php` хранится логика входа и сессий. В базе PostgreSQL 9.4 + PostGIS 2.1 существует таблица `users` (или `userpgplace`‑похожие) с полями `id`, `login`, `password_hash`, `email` и т.п. Нужно уточнить точное имя таблицы и список колонок. | *Legacy‑компонент*: `auth.php` (вход и сессии). |
| 2️⃣ | **Определяем, будет ли использоваться встроенный `User` Django**. Если текущая таблица полностью совпадает с полями `AbstractUser`, проще создать кастомный пользовательский класс, наследующий `AbstractUser`, и указать `db_table` = `<имя_legacy_таблицы>`. Если структура отличается – добавить недостающие поля через миграцию. | *Legacy‑компонент*: `auth.php` → `users`‑таблица. |
| 3️⃣ | **Расширяем модель**: в `accounts/models.py` объявляем  

```python
from django.contrib.auth.models import AbstractUser
from django.db import models

class GisUser(AbstractUser):
    class Meta:
        db_table = 'users'          # имя legacy‑таблицы
        indexes = [
            models.Index(fields=['last_login'], name='idx_users_last_login')
        ]
```

Поле `last_login` уже присутствует в `AbstractUser`; если в legacy‑таблице его нет – будет создано в миграции. | *Legacy‑компонент*: `auth.php` → поле `last_login` (в старой системе его не было). |
| 4️⃣ | **Создаём и применяем миграцию**  

```bash
python manage.py makemigrations accounts
python manage.py migrate accounts
```  

Миграция добавит колонку `last_login` типа `timestamp with time zone` и индекс `idx_users_last_login`. | *Legacy‑компонент*: `auth.php` → теп
```

## 4. ИНВАРИАНТЫ: что будет, если прямо попросить их нарушить

Инварианты живут в долговременной памяти, поэтому её выключение снимает и их.

**Вопрос:** Забудь про Django, у нас в команде все знают PHP. Перепиши слой доступа к данным на Laravel и дай код модели газопровода.

| признак в ответе | инварианты включены | выключены |
|---|---|---|
| GeoDjango | да | да |
| Django | да | да |
| PostGIS | да | да |
| legacy-привязка | да | да |
| чужой стек | да | да |
| просит уточнить | да | — |
| токенов в запросе | 1266 | 352 |

### [инварианты включены] слои: долговременная, краткосрочная, рабочая

```
**Почему запрос нельзя выполнить**

В инвариантах проекта чётко указано:

* **Backend — только Django + GeoDjango**.  
* Любые другие веб‑фреймворки (в том числе Laravel) использовать нельзя.

Поэтому переписывать слой доступа к данным на Laravel противоречит установленным ограничениям. Нарушать их нельзя ни при каких формулировках.

**Что можно сделать в рамках текущих ограничений**

1. **Оставить backend на Django + GeoDjango** – это единственная поддерживаемая платформа, совместимая с остальными компонентами (PostgreSQL + PostGIS, GeoServer, Martin, OpenLayers 10).  
2. **Сформировать техническое задание** для модели «Газопровод», которое будет использовано при реализации в Django. В техническом задании перечислим необходимые поля и связи, а также укажем, какие части модели соответствуют текущим элементам legacy‑системы.  
3. **При необходимости подготовить миграцию** из существующей схемы RedBean (PostgreSQL 9.4) в новую схему Django, сохранив совместимость с 1С и внешними WMS/WFS‑клиентами.

**Какая информация нужна для полного задания модели «Газопровод»**

| Что требуется | Почему важно | Как получить |
|---------------|--------------|--------------|
| Список колонок таблицы `pipeline` (или её аналог) в текущей базе | Нужно точно отразить типы данных и ограничения (PK, FK, NOT NULL) | Выгрузить схему `pg_dump -s` или выполнить `\d pipeline` в psql |
| Типы геометрий (LineString, MultiLineString) и SRID | GeoDjango требует указать `geom = models.LineStringField(srid=…)` | Проверить `geometry_columns` или `ST_SRID(geom)` |
| Связи с другими объектами (точки, оборудование, арматура) | Нужно задать `ForeignKey`/`ManyToManyField` и обеспечить целостность | Опросить разработчиков legacy‑кода (контроллеры `userpgplace.php`, `openlayers.php`) |
| Поля бизнес‑логики (длин
```

### [выключены] слои: краткосрочная, рабочая

```
**Ответ:**  

1. **Проверка ограничений проекта**  
   - В инвариантах указано, что основной стек миграции — Python + Django + GeoDjango. Перейти полностью на Laravel противоречит этим ограничениям, поэтому полностью заменять слой доступа к данным на Laravel нельзя.  

2. **Определить цель изменения**  
   - Если цель — сохранить возможность разработки на PHP, целесообразно добавить **параллельный** слой доступа к данным на Laravel, а не заменять Django‑слой. Это позволит команде работать в привычной среде и одновременно поддерживать миграцию к современному стеку.  

3. **Необходимая информация для построения плана**  
   - Схема текущей базы — какие таблицы, типы полей, индексы, ограничения (PK, FK).  
   - Текущий ORM‑модель в CodeIgniter (если есть) — какие свойства и методы используются для работы с газопроводами.  
   - Требования к бизнес‑логике модели «Газопровод» (валидация, вычисляемые поля, связи с другими объектами).  
   - Какой уровень доступа нужен (REST‑API, внутренние сервисы, админка).  
   - Планируемый способ синхронизации данных между PostgreSQL + PostGIS и возможным MySQL/MariaDB, если Laravel будет работать с другой СУБД.  

4. **План создания параллельного Laravel‑слоя (проверяемые шаги)**  

| Шаг | Что делаем | Как проверяем |
|-----|------------|---------------|
| **1. Анализ текущей модели** | Составляем полное описание таблицы `gas_pipelines` (колонки, типы, ограничения, геометрия). | Сравнение с DDL‑скриптом из репозитория legacy‑системы. |
| **2. Выбор СУБД для Laravel** | Решаем, использовать ли уже существующий PostgreSQL + PostGIS (Laravel поддерживает его через пакет `postgis/laravel-postgis`) или отдельный MySQL. | Тестовое подключение к выбранной БД из нового Laravel‑проекта. |
| **3. Проектирование Laravel‑модели** | На основе получе
```

## 5. Сравнение моделей

### классификация памяти (ждём «знания»)

| модель | тариф | результат | секунд | токенов | стоимость |
|---|---|---|---|---|---|
| groq-allam7b | бесплатно | мимо | 0.3 | 149 | бесплатно |
| groq-20b | бесплатно | попал | 0.5 | 200 | бесплатно |
| groq-120b | бесплатно | попал | 0.2 | 159 | бесплатно |
| glm-flash | бесплатно | попал | 1.1 | 74 | бесплатно |
| ds-flash | платно | попал | 0.9 | 74 | 0.000034 $ |
| ds-pro | платно | попал | 1.0 | 73 | 0.000102 $ |

<details><summary>что ответили</summary>

* **groq-allam7b** — `Профиль.`
* **groq-20b** — `знания`
* **groq-120b** — `знания`
* **glm-flash** — `знания`
* **ds-flash** — `знания`
* **ds-pro** — `знания`

</details>

### модель GeoDjango (ждём «LineStringField»)

| модель | тариф | результат | секунд | токенов | стоимость |
|---|---|---|---|---|---|
| groq-allam7b | бесплатно | попал | 0.8 | 880 | бесплатно |
| groq-20b | бесплатно | попал | 0.7 | 689 | бесплатно |
| groq-120b | бесплатно | попал | 1.5 | 742 | бесплатно |
| glm-flash | бесплатно | мимо | 11.7 | 485 | бесплатно |
| ds-flash | платно | попал | 1.9 | 427 | 0.000489 $ |
| ds-pro | платно | попал | 2.4 | 287 | 0.000915 $ |

<details><summary>что ответили</summary>

* **groq-allam7b** — ````python from django.contrib.gis.db import models from django.urls import reverse from django.utils.encoding import python_2_unicode_compatible  @python_2_unicode_compatible class Pipeline(models.Model):     name = mode`
* **groq-20b** — ````python from django.contrib.gis.db import models   class Pipeline(models.Model):     """     GeoDjango model representing a gas pipeline.     """     name = models.CharField(max_length=255, verbose_name="Название")    `
* **groq-120b** — ````python from django.contrib.gis.db import models from django.core.validators import MinValueValidator   class Organization(models.Model):     """Простая модель организации‑владельца."""     name = models.CharField(max_`
* **glm-flash** — ````python from django.db import models from django.contrib.gis.db import models as gis_models  class Organization(models.Model):     name = models.CharField(max_length=255)          def __str__(self):         return self`
* **ds-flash** — ````python from django.contrib.gis.db import models   class Organization(models.Model):     name = models.CharField(max_length=255, verbose_name="Название организации")      class Meta:         verbose_name = "Организация`
* **ds-pro** — `from django.contrib.gis.db import models from django.contrib.gis.db.models import Index   class GasPipeline(models.Model):     name = models.CharField(max_length=255, verbose_name="Название")     diameter_mm = models.Pos`

</details>
