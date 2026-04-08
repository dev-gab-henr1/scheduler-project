"""
Cliente ClickUp API v2 - busca tasks e extrai campos customizados.
Reutilizavel por qualquer script do projeto.
"""

import os
import time
import logging
from typing import Optional

import requests

from scripts.retry_utils import (
    RetryableError,
    execute_with_retry,
    load_retry_config_from_env,
)

logger = logging.getLogger("scheduler.clickup_client")

# Configuracao
CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN", "")
CLICKUP_BASE_URL = os.getenv("CLICKUP_BASE_URL", "https://api.clickup.com/api/v2")
CLICKUP_TIMEOUT_SECONDS = int(os.getenv("CLICKUP_TIMEOUT_SECONDS", "30"))
CLICKUP_RETRY_CONFIG = load_retry_config_from_env("CLICKUP")


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


def _request_with_retry(
    url: str,
    params: dict,
    list_id_short: str,
    archived: bool,
    current_page: int,
) -> requests.Response:
    """Executa request ao ClickUp com retry para falhas transitórias."""

    def _request_once() -> requests.Response:
        try:
            resp = requests.get(
                url,
                headers=_headers(),
                params=params,
                timeout=CLICKUP_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise RetryableError(f"erro de conexao: {type(exc).__name__}") from exc

        status = resp.status_code
        if status == 429:
            retry_after = float(resp.headers.get("Retry-After", "5"))
            raise RetryableError("rate limit 429", retry_after=retry_after)

        if status in {408, 500, 502, 503, 504}:
            raise RetryableError(f"status transitório {status}")

        resp.raise_for_status()
        return resp

    return execute_with_retry(
        action_label=f"clickup_list_{list_id_short}_archived_{archived}_page_{current_page}",
        func=_request_once,
        logger=logger,
        config=CLICKUP_RETRY_CONFIG,
        retry_exceptions=(RetryableError,),
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

        # Respeita rate limit (aprox. 100 req/min).
        time.sleep(0.7)

    logger.info(
        "ClickUp | list=%s | archived=%s | coleta concluida | total_tasks=%s",
        list_id_short,
        archived,
        len(all_tasks),
    )
    return all_tasks


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


def extract_custom_field(task: dict, field_id: str, default: str = "") -> str:
    """Extrai o valor de um campo customizado de uma task."""
    for field in task.get("custom_fields", []):
        if field.get("id") == field_id:
            val = field.get("value")
            if val is None:
                return default
            # Campos de tipo drop_down retornam indice numerico.
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
