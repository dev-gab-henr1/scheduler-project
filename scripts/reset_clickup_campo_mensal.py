"""
Reset mensal de campo dropdown no ClickUp.

Objetivo:
- No fechamento/virada de mes, forcar o campo dropdown para "Nao".
- Campo alvo default: b9aee798-9dc5-449f-8486-88fad07775ca
"""

import logging
import os
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from scripts.clickup_client import iter_tasks_from_list, set_task_custom_field_value

logger = logging.getLogger("scheduler.reset_clickup_campo_mensal")

RESET_FIELD_ID = os.getenv(
    "CLICKUP_RESET_FIELD_ID",
    "b9aee798-9dc5-449f-8486-88fad07775ca",
).strip()
RESET_LABEL_SIM = os.getenv("CLICKUP_RESET_LABEL_SIM", "Sim").strip()
RESET_LABEL_NAO = os.getenv("CLICKUP_RESET_LABEL_NAO", "Nao").strip()
# Lista alvo fixa: Ongoing
# URL informada: https://app.clickup.com/9013290037/v/l/6-901322296001-1
# List ID esperado: 901322296001
RESET_TARGET_LIST_ID = os.getenv(
    "CLICKUP_RESET_TARGET_LIST_ID",
    os.getenv("CLICKUP_LIST_ONGOING", "901322296001"),
).strip()
RESET_INCLUDE_CLOSED = os.getenv("CLICKUP_RESET_INCLUDE_CLOSED", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
RESET_INCLUDE_ARCHIVED = os.getenv(
    "CLICKUP_RESET_INCLUDE_ARCHIVED", "false"
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
RESET_DRY_RUN = os.getenv("CLICKUP_RESET_DRY_RUN", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
RESET_PROGRESS_EVERY = max(1, int(os.getenv("CLICKUP_RESET_PROGRESS_EVERY", "50")))
RESET_UPDATE_WORKERS = max(1, int(os.getenv("CLICKUP_RESET_UPDATE_WORKERS", "8")))


def _normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _resolve_list_ids() -> list[str]:
    if RESET_TARGET_LIST_ID:
        return [RESET_TARGET_LIST_ID]
    return []


def _find_field(task: dict, field_id: str) -> Optional[dict]:
    for field in task.get("custom_fields", []):
        if str(field.get("id", "")).strip() == field_id:
            return field
    return None


def _resolve_dropdown_option_ids(field: dict) -> tuple[Optional[str], Optional[str]]:
    options = field.get("type_config", {}).get("options", []) or []
    sim_id = None
    nao_id = None
    for option in options:
        option_id = str(option.get("id", "")).strip()
        option_name = _normalize_text(option.get("name", ""))
        if not option_id:
            continue
        if option_name == _normalize_text(RESET_LABEL_SIM):
            sim_id = option_id
        if option_name == _normalize_text(RESET_LABEL_NAO):
            nao_id = option_id
    return sim_id, nao_id


def _resolve_dropdown_option_name_by_value(field: dict, raw_value) -> str:
    """
    Resolve o nome da opcao do dropdown a partir do valor atual do campo.
    O ClickUp pode retornar o valor como ID da opcao ou como orderindex.
    """
    if raw_value is None:
        return ""

    value = str(raw_value).strip()
    if not value:
        return ""

    options = field.get("type_config", {}).get("options", []) or []

    for option in options:
        option_id = str(option.get("id", "")).strip()
        if option_id and value == option_id:
            return _normalize_text(option.get("name", ""))

    for option in options:
        orderindex = str(option.get("orderindex", "")).strip()
        if orderindex and value == orderindex:
            return _normalize_text(option.get("name", ""))

    if value.isdigit():
        idx = int(value)
        if 0 <= idx < len(options):
            return _normalize_text(options[idx].get("name", ""))

    return ""


def _update_task_to_nao(task_id: str, nao_id: str) -> tuple[str, bool]:
    try:
        set_task_custom_field_value(task_id, RESET_FIELD_ID, nao_id)
        return task_id, True
    except Exception:
        logger.exception(
            "Reset mensal ClickUp | falha ao atualizar task=%s",
            task_id,
        )
        return task_id, False


def tarefa_reset_clickup_campo_mensal() -> dict:
    """
    Executa o reset mensal do campo dropdown (Sim -> Nao).
    """
    list_ids = _resolve_list_ids()
    if not RESET_FIELD_ID:
        return {
            "_aborted": True,
            "_abort_reason": "CLICKUP_RESET_FIELD_ID nao definido",
        }
    if not list_ids:
        return {
            "_aborted": True,
            "_abort_reason": "lista alvo Ongoing nao definida (CLICKUP_RESET_TARGET_LIST_ID)",
        }

    logger.info(
        "Reset mensal ClickUp | inicio | field=%s | listas=%s | include_closed=%s | include_archived=%s | dry_run=%s",
        RESET_FIELD_ID,
        len(list_ids),
        RESET_INCLUDE_CLOSED,
        RESET_INCLUDE_ARCHIVED,
        RESET_DRY_RUN,
    )

    unique_task_ids = set()
    inspected = 0
    missing_field = 0
    updated = 0
    already_nao = 0
    failed_updates = 0
    option_not_found = 0
    skipped_unexpected = 0
    failed_task_ids = []
    pending_update_task_ids = []
    sim_name = _normalize_text(RESET_LABEL_SIM)
    nao_name = _normalize_text(RESET_LABEL_NAO)

    for list_id in list_ids:
        logger.info("Reset mensal ClickUp | processando lista=%s", list_id)
        started = time.perf_counter()

        for task in iter_tasks_from_list(
            list_id=list_id,
            include_closed=RESET_INCLUDE_CLOSED,
            include_archived=RESET_INCLUDE_ARCHIVED,
        ):
            task_id = str(task.get("id", "")).strip()
            if not task_id or task_id in unique_task_ids:
                continue
            unique_task_ids.add(task_id)
            inspected += 1

            field = _find_field(task, RESET_FIELD_ID)
            if not field:
                missing_field += 1
                continue
            if str(field.get("type", "")).strip() != "drop_down":
                missing_field += 1
                continue

            sim_id, nao_id = _resolve_dropdown_option_ids(field)
            if not nao_id:
                option_not_found += 1
                continue

            current_raw_value = field.get("value")
            current_value = str(current_raw_value).strip() if current_raw_value is not None else ""
            current_name = _resolve_dropdown_option_name_by_value(field, current_raw_value)

            if current_value == nao_id or current_name == nao_name:
                already_nao += 1
                continue

            # Atualiza somente quando valor atual for Sim ou vazio.
            if current_value != "" and current_value != sim_id and current_name != sim_name:
                skipped_unexpected += 1
                continue

            pending_update_task_ids.append((task_id, nao_id))

            if inspected % RESET_PROGRESS_EVERY == 0:
                elapsed = max(time.perf_counter() - started, 0.001)
                rate = inspected / elapsed
                logger.info(
                    "Reset mensal ClickUp | progresso leitura | inspected=%s | pendentes=%s | already_nao=%s | missing_field=%s | taxa=%.1f tasks/s",
                    inspected,
                    len(pending_update_task_ids),
                    already_nao,
                    missing_field,
                    rate,
                )

    if pending_update_task_ids:
        logger.info(
            "Reset mensal ClickUp | atualizacao | pendentes=%s | workers=%s | dry_run=%s",
            len(pending_update_task_ids),
            RESET_UPDATE_WORKERS,
            RESET_DRY_RUN,
        )

    if RESET_DRY_RUN:
        updated = len(pending_update_task_ids)
    elif pending_update_task_ids:
        with ThreadPoolExecutor(max_workers=RESET_UPDATE_WORKERS) as executor:
            future_to_task = {
                executor.submit(_update_task_to_nao, task_id, nao_id): task_id
                for task_id, nao_id in pending_update_task_ids
            }
            for idx, future in enumerate(as_completed(future_to_task), start=1):
                task_id, ok = future.result()
                if ok:
                    updated += 1
                else:
                    failed_updates += 1
                    failed_task_ids.append(task_id)

                if idx % RESET_PROGRESS_EVERY == 0 or idx == len(pending_update_task_ids):
                    logger.info(
                        "Reset mensal ClickUp | progresso atualizacao | concluidas=%s/%s | sucesso=%s | falhas=%s",
                        idx,
                        len(pending_update_task_ids),
                        updated,
                        failed_updates,
                    )

    logger.info(
        "Reset mensal ClickUp | fim | inspected=%s | pending=%s | updated=%s | already_nao=%s | missing_field=%s | option_not_found=%s | skipped_unexpected=%s | failed_updates=%s | dry_run=%s",
        inspected,
        len(pending_update_task_ids),
        updated,
        already_nao,
        missing_field,
        option_not_found,
        skipped_unexpected,
        failed_updates,
        RESET_DRY_RUN,
    )

    return {
        "_aborted": False,
        "inspected": inspected,
        "updated": updated,
        "already_nao": already_nao,
        "missing_field": missing_field,
        "option_not_found": option_not_found,
        "skipped_unexpected": skipped_unexpected,
        "failed_updates": failed_updates,
        "failed_task_ids": failed_task_ids,
        "dry_run": RESET_DRY_RUN,
    }
