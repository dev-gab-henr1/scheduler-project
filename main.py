"""
Scheduler Central - executa apenas a rotina da Copel.
Resiliente para execucao continua em servidor.
"""

import os
import time
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from scripts.atualizacao_copel import tarefa_atualizacao_copel
from scripts.retry_utils import RetryConfig, backoff_seconds

# Carrega variaveis de ambiente
load_dotenv()

# Configuracao de logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("scheduler")

# Curitiba utiliza o fuso IANA America/Sao_Paulo
TIMEZONE = "America/Sao_Paulo"
COPEL_TRIGGER = CronTrigger(hour=7, minute=0, timezone=TIMEZONE)

# Backoff de reinicio para proteger em caso de falhas repetidas.
RESTART_BASE_SECONDS = float(os.getenv("SCHEDULER_RESTART_BASE_SECONDS", "5"))
RESTART_MAX_SECONDS = float(os.getenv("SCHEDULER_RESTART_MAX_SECONDS", "300"))
RESTART_MAX_EXPONENT = int(os.getenv("SCHEDULER_RESTART_MAX_EXPONENT", "6"))
RESTART_BACKOFF_CONFIG = RetryConfig(
    max_retries=0,
    base_seconds=RESTART_BASE_SECONDS,
    max_seconds=RESTART_MAX_SECONDS,
)


def _scheduler_restart_delay_seconds(restart_attempt: int) -> float:
    """
    Calcula atraso de reinicio com exponencial limitado.
    Mantem a mesma semantica anterior: primeira falha aguarda base * 2.
    """
    bounded_attempt = min(max(restart_attempt + 1, 1), RESTART_MAX_EXPONENT + 1)
    return backoff_seconds(RESTART_BACKOFF_CONFIG, bounded_attempt)


def executar_tarefa(nome: str, func, *args, **kwargs):
    """Executa tarefa com logs de inicio/fim e tratamento de erro."""
    logger.info("Iniciando: %s", nome)
    inicio = datetime.now()
    try:
        result = func(*args, **kwargs)
        if isinstance(result, dict) and result.get("_aborted"):
            duracao = (datetime.now() - inicio).total_seconds()
            logger.error(
                "Abortada: %s (%.1fs) | motivo=%s",
                nome,
                duracao,
                result.get("_abort_reason", "nao informado"),
            )
            return False
        duracao = (datetime.now() - inicio).total_seconds()
        logger.info("Concluida: %s (%.1fs)", nome, duracao)
        return True
    except Exception as exc:
        duracao = (datetime.now() - inicio).total_seconds()
        logger.error("Erro em %s apos %.1fs: %s", nome, duracao, exc, exc_info=True)
        return False


def _build_scheduler() -> BlockingScheduler:
    tzinfo = ZoneInfo(TIMEZONE)
    scheduler = BlockingScheduler(
        timezone=TIMEZONE,
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 6 * 60 * 60,
        },
    )

    state = {
        "fallback_scheduled_for": None,  # date
        "fallback_executed_for": None,   # date
    }

    def _run_copel_job(is_fallback: bool = False):
        now_local = datetime.now(tzinfo)
        run_day = now_local.date()
        run_kind = "fallback" if is_fallback else "principal"
        logger.info("Copel | execucao %s | data=%s", run_kind, run_day.isoformat())

        ok = executar_tarefa("Atualizacao Relatorios Copel", tarefa_atualizacao_copel)

        if is_fallback:
            state["fallback_executed_for"] = run_day
            if not ok:
                logger.error(
                    "Copel | fallback falhou em %s. Nova tentativa apenas no proximo agendamento diario.",
                    run_day.isoformat(),
                )
            return

        # Execucao principal (07:00): se falhar, agenda apenas 1 fallback para +1h.
        if ok:
            return

        already_scheduled = state.get("fallback_scheduled_for") == run_day
        already_executed = state.get("fallback_executed_for") == run_day
        if already_scheduled or already_executed:
            logger.warning(
                "Copel | fallback ja tratado para %s. Nenhuma nova tentativa hoje.",
                run_day.isoformat(),
            )
            return

        fallback_run_at = now_local + timedelta(hours=1)
        scheduler.add_job(
            _run_copel_job,
            trigger=DateTrigger(run_date=fallback_run_at, timezone=TIMEZONE),
            kwargs={"is_fallback": True},
            id="atualizacao_copel_fallback",
            name="Atualizacao Relatorios Copel (Fallback +1h)",
            replace_existing=True,
        )
        state["fallback_scheduled_for"] = run_day
        logger.warning(
            "Copel | falha na execucao principal. Fallback agendado para %s.",
            fallback_run_at.strftime("%Y-%m-%d %H:%M:%S %Z"),
        )

    scheduler.add_job(
        _run_copel_job,
        trigger=COPEL_TRIGGER,
        kwargs={"is_fallback": False},
        id="atualizacao_copel",
        name="Atualizacao Relatorios Copel",
        replace_existing=True,
    )
    return scheduler


def main():
    restart_attempt = 0

    while True:
        scheduler = None
        restart_delay_seconds = RESTART_BASE_SECONDS
        try:
            scheduler = _build_scheduler()
            logger.info("Agendada: Atualizacao Relatorios Copel -> %s", COPEL_TRIGGER)
            logger.info("Scheduler iniciado com 1 tarefa(s) | TZ: %s", TIMEZONE)
            scheduler.start()

            # Em condicoes normais o start() nao retorna.
            restart_attempt += 1
            restart_delay_seconds = _scheduler_restart_delay_seconds(restart_attempt)
            logger.error(
                "Scheduler encerrou inesperadamente sem excecao. Reiniciando em %.1fs.",
                restart_delay_seconds,
            )
        except (KeyboardInterrupt, SystemExit):
            logger.info("Scheduler encerrado por sinal do sistema.")
            break
        except Exception:
            restart_attempt += 1
            restart_delay_seconds = _scheduler_restart_delay_seconds(restart_attempt)
            logger.exception(
                "Falha critica no scheduler (tentativa %s). Reiniciando em %.1fs.",
                restart_attempt,
                restart_delay_seconds,
            )
        finally:
            if scheduler is not None:
                try:
                    scheduler.shutdown(wait=False)
                except Exception:
                    pass

        time.sleep(restart_delay_seconds)


if __name__ == "__main__":
    main()
