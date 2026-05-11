"""
Cliente ClickUp API v2 - busca tasks e extrai campos customizados.
Reutilizavel por qualquer script do projeto.
"""

import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

from scripts.retry_utils import (
    RetryableError,
    execute_with_retry,
    load_retry_config_from_env,
)

logger = logging.getLogger("scheduler.clickup_client")

# Garante carregamento de variaveis ao executar scripts isolados (fora do main.py).
PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

# Configuracao
CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN", "")
CLICKUP_BASE_URL = os.getenv("CLICKUP_BASE_URL", "https://api.clickup.com/api/v2")
CLICKUP_TIMEOUT_SECONDS = int(os.getenv("CLICKUP_TIMEOUT_SECONDS", "30"))
CLICKUP_RETRY_CONFIG = load_retry_config_from_env("CLICKUP")
CLICKUP_LIST_PAGE_DELAY_SECONDS = float(
    os.getenv("CLICKUP_LIST_PAGE_DELAY_SECONDS", "0.7")
)


def _headers() -> dict:
    if not CLICKUP_TOKEN:
        raise ValueError("CLICKUP_API_TOKEN nao definido no .env")
    return {"Authorization": CLICKUP_TOKEN}


def _short_id(value: str, keep: int = 6) -> str:
    """Retorna uma versao curta de IDs para logs."""
    if not value:
        return "(vazio)"
    text = str(value)
    if len(text) <= keep:
        return text
    return f"...{text[-keep:]}"


def _request_json_with_retry(
    method: str,
    url: str,
    action_label: str,
    params: Optional[dict] = None,
    json_body: Optional[dict] = None,
) -> requests.Response:
    """Executa request HTTP no ClickUp com retry para falhas transitorias."""

    def _request_once() -> requests.Response:
        try:
            resp = requests.request(
                method=method,
                url=url,
                headers=_headers(),
                params=params,
                json=json_body,
                timeout=CLICKUP_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise RetryableError(f"erro de conexao: {type(exc).__name__}") from exc

        status = resp.status_code
        if status == 429:
            retry_after = float(resp.headers.get("Retry-After", "5"))
            raise RetryableError("rate limit 429", retry_after=retry_after)

        if status in {408, 409, 423, 425, 500, 502, 503, 504}:
            raise RetryableError(f"status transitorio {status}")

        resp.raise_for_status()
        return resp

    return execute_with_retry(
        action_label=action_label,
        func=_request_once,
        logger=logger,
        config=CLICKUP_RETRY_CONFIG,
        retry_exceptions=(RetryableError,),
    )


def _request_with_retry(
    url: str,
    params: dict,
    list_id_short: str,
    archived: bool,
    current_page: int,
) -> requests.Response:
    """Executa request de listagem no ClickUp com retry."""
    return _request_json_with_retry(
        method="GET",
        url=url,
        action_label=f"clickup_list_{list_id_short}_archived_{archived}_page_{current_page}",
        params=params,
    )


def _fetch_tasks_from_list_mode(
    list_id: str,
    include_closed: bool,
    archived: bool,
    page: int = 0,
    custom_field_ids: Optional[list[str]] = None,
) -> list[dict]:
    """Busca tasks de uma lista para um modo especifico de arquivamento."""
    all_tasks = []
    current_page = page
    list_id_short = _short_id(list_id)

    while True:
        params = {
            "page": current_page,
            "include_closed": str(include_closed).lower(),
            "subtasks": "false",
            "archived": str(archived).lower(),
        }
        if custom_field_ids:
            params["custom_fields"] = custom_field_ids

        url = f"{CLICKUP_BASE_URL}/list/{list_id}/task"
        started = time.perf_counter()
        resp = _request_with_retry(url, params, list_id_short, archived, current_page)
        elapsed = time.perf_counter() - started
        data = resp.json()
        tasks = data.get("tasks", [])

        logger.info(
            "ClickUp | list=%s | archived=%s | page=%s | status=%s | tasks=%s | %.2fs",
            list_id_short,
            archived,
            current_page,
            resp.status_code,
            len(tasks),
            elapsed,
        )

        if not tasks:
            break

        all_tasks.extend(tasks)
        current_page += 1

        # Delay entre paginas para reduzir risco de rate limit (configuravel por env).
        if CLICKUP_LIST_PAGE_DELAY_SECONDS > 0:
            time.sleep(CLICKUP_LIST_PAGE_DELAY_SECONDS)

    logger.info(
        "ClickUp | list=%s | archived=%s | coleta concluida | total_tasks=%s",
        list_id_short,
        archived,
        len(all_tasks),
    )
    return all_tasks


def _iter_tasks_from_list_mode(
    list_id: str,
    include_closed: bool,
    archived: bool,
    page: int = 0,
    custom_field_ids: Optional[list[str]] = None,
):
    """
    Itera tasks de uma lista por pagina, sem acumular tudo em memoria.
    """
    current_page = page
    list_id_short = _short_id(list_id)
    yielded = 0

    while True:
        params = {
            "page": current_page,
            "include_closed": str(include_closed).lower(),
            "subtasks": "false",
            "archived": str(archived).lower(),
        }
        if custom_field_ids:
            params["custom_fields"] = custom_field_ids

        url = f"{CLICKUP_BASE_URL}/list/{list_id}/task"
        started = time.perf_counter()
        resp = _request_with_retry(url, params, list_id_short, archived, current_page)
        elapsed = time.perf_counter() - started
        data = resp.json()
        tasks = data.get("tasks", [])

        logger.info(
            "ClickUp | list=%s | archived=%s | page=%s | status=%s | tasks=%s | %.2fs",
            list_id_short,
            archived,
            current_page,
            resp.status_code,
            len(tasks),
            elapsed,
        )

        if not tasks:
            break

        for task in tasks:
            yielded += 1
            yield task

        current_page += 1

        if CLICKUP_LIST_PAGE_DELAY_SECONDS > 0:
            time.sleep(CLICKUP_LIST_PAGE_DELAY_SECONDS)

    logger.info(
        "ClickUp | list=%s | archived=%s | iteracao concluida | yielded=%s",
        list_id_short,
        archived,
        yielded,
    )


def iter_tasks_from_list(
    list_id: str,
    include_closed: bool = False,
    include_archived: bool = False,
    page: int = 0,
    custom_field_ids: Optional[list[str]] = None,
):
    """
    Itera tasks de uma lista (paginacao automatica) com deduplicacao por ID.
    Evita manter todas as tasks em memoria.
    """
    if not list_id:
        logger.warning("ClickUp | list_id vazio, nenhuma task sera buscada.")
        return

    list_id_short = _short_id(list_id)
    logger.info(
        "ClickUp | list=%s | iniciando iteracao (include_closed=%s, include_archived=%s)",
        list_id_short,
        include_closed,
        include_archived,
    )

    seen_ids = set()
    yielded_final = 0

    if include_archived:
        iterables = [
            _iter_tasks_from_list_mode(
                list_id=list_id,
                include_closed=include_closed,
                archived=True,
                page=page,
                custom_field_ids=custom_field_ids,
            ),
            _iter_tasks_from_list_mode(
                list_id=list_id,
                include_closed=include_closed,
                archived=False,
                page=page,
                custom_field_ids=custom_field_ids,
            ),
        ]
    else:
        iterables = [
            _iter_tasks_from_list_mode(
                list_id=list_id,
                include_closed=include_closed,
                archived=False,
                page=page,
                custom_field_ids=custom_field_ids,
            )
        ]

    for iterable in iterables:
        for task in iterable:
            task_id = task.get("id")
            if task_id and task_id in seen_ids:
                continue
            if task_id:
                seen_ids.add(task_id)
            yielded_final += 1
            yield task

    logger.info(
        "ClickUp | list=%s | iteracao finalizada | total=%s",
        list_id_short,
        yielded_final,
    )


def get_tasks_from_list(
    list_id: str,
    include_closed: bool = False,
    include_archived: bool = False,
    page: int = 0,
    custom_field_ids: Optional[list[str]] = None,
) -> list[dict]:
    """
    Busca todas as tasks de uma lista (paginando automaticamente).
    Retorna lista de dicts com dados brutos da API.
    """
    if not list_id:
        logger.warning("ClickUp | list_id vazio, nenhuma task sera buscada.")
        return []

    list_id_short = _short_id(list_id)

    logger.info(
        "ClickUp | list=%s | iniciando coleta (include_closed=%s, include_archived=%s)",
        list_id_short,
        include_closed,
        include_archived,
    )

    archived_tasks = []
    if include_archived:
        # Carrega arquivadas primeiro para que, em deduplicacao, nao-arquivadas prevalecam.
        archived_tasks = _fetch_tasks_from_list_mode(
            list_id=list_id,
            include_closed=include_closed,
            archived=True,
            page=page,
            custom_field_ids=custom_field_ids,
        )

    non_archived_tasks = _fetch_tasks_from_list_mode(
        list_id=list_id,
        include_closed=include_closed,
        archived=False,
        page=page,
        custom_field_ids=custom_field_ids,
    )

    combined_tasks = archived_tasks + non_archived_tasks

    # Evita duplicacao por ID ao combinar arquivadas e nao-arquivadas.
    deduped_tasks = []
    seen_ids = set()
    for task in combined_tasks:
        task_id = task.get("id")
        if task_id:
            if task_id in seen_ids:
                continue
            seen_ids.add(task_id)
        deduped_tasks.append(task)

    logger.info(
        "ClickUp | list=%s | coleta finalizada | total=%s (arquivadas=%s, nao_arquivadas=%s)",
        list_id_short,
        len(deduped_tasks),
        len(archived_tasks),
        len(non_archived_tasks),
    )
    return deduped_tasks


def set_task_custom_field_value(
    task_id: str,
    field_id: str,
    value,
    value_options: Optional[dict] = None,
) -> dict:
    """
    Define o valor de um custom field em uma task.
    Endpoint: POST /task/{task_id}/field/{field_id}
    """
    if not task_id:
        raise ValueError("task_id vazio para set_task_custom_field_value")
    if not field_id:
        raise ValueError("field_id vazio para set_task_custom_field_value")

    payload = {"value": value}
    if value_options is not None:
        payload["value_options"] = value_options

    task_id_short = _short_id(task_id)
    field_id_short = _short_id(field_id)
    logger.info(
        "ClickUp | atualizando custom field | task=%s | field=%s",
        task_id_short,
        field_id_short,
    )

    started = time.perf_counter()
    resp = _request_json_with_retry(
        method="POST",
        url=f"{CLICKUP_BASE_URL}/task/{task_id}/field/{field_id}",
        action_label=f"clickup_set_field_task_{task_id_short}_field_{field_id_short}",
        json_body=payload,
    )
    elapsed = time.perf_counter() - started
    logger.info(
        "ClickUp | custom field atualizado | task=%s | field=%s | status=%s | %.2fs",
        task_id_short,
        field_id_short,
        resp.status_code,
        elapsed,
    )
    return resp.json() if resp.content else {}


def extract_custom_field(task: dict, field_id: str, default: str = "") -> str:
    """Extrai o valor de um campo customizado de uma task."""
    for field in task.get("custom_fields", []):
        if field.get("id") == field_id:
            val = field.get("value")
            if val is None:
                return default
            # Campos de tipo drop_down podem retornar indice numerico.
            if field.get("type") == "drop_down" and isinstance(val, (int, float)):
                options = field.get("type_config", {}).get("options", [])
                idx = int(val)
                if 0 <= idx < len(options):
                    return options[idx].get("name", default)
                return default
            return str(val)
    return default


def get_task_name_prefix(task: dict, separator: str = " - ") -> str:
    """Retorna a parte do nome da task antes do separador."""
    name = task.get("name", "")
    if separator in name:
        return name.split(separator, 1)[0].strip()
    return name.strip()


def get_task_status(task: dict) -> str:
    """Retorna o status textual da task."""
    status = task.get("status", {})
    return status.get("status", "").strip()
