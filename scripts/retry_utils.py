"""
Utilitarios de retry/backoff reutilizaveis entre scripts.
"""

import os
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple, Type, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class RetryConfig:
    max_retries: int = 8
    base_seconds: float = 2.0
    max_seconds: float = 60.0


class RetryableError(Exception):
    """Erro marcado como elegivel para retry."""

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


def load_retry_config_from_env(
    prefix: str,
    default_max_retries: int = 8,
    default_base_seconds: float = 2.0,
    default_max_seconds: float = 60.0,
) -> RetryConfig:
    """
    Carrega configuracao de retry de variaveis de ambiente com prefixo.
    Exemplo para prefixo CLICKUP:
      CLICKUP_MAX_RETRIES
      CLICKUP_RETRY_BASE_SECONDS
      CLICKUP_RETRY_MAX_SECONDS
    """
    max_retries = int(os.getenv(f"{prefix}_MAX_RETRIES", str(default_max_retries)))
    base_seconds = float(
        os.getenv(f"{prefix}_RETRY_BASE_SECONDS", str(default_base_seconds))
    )
    max_seconds = float(
        os.getenv(f"{prefix}_RETRY_MAX_SECONDS", str(default_max_seconds))
    )
    return RetryConfig(
        max_retries=max_retries,
        base_seconds=base_seconds,
        max_seconds=max_seconds,
    )


def backoff_seconds(
    config: RetryConfig,
    attempt: int,
    retry_after: Optional[float] = None,
) -> float:
    if retry_after is not None and retry_after > 0:
        return min(config.max_seconds, retry_after)
    return min(config.max_seconds, config.base_seconds * (2 ** max(attempt - 1, 0)))


def execute_with_retry(
    action_label: str,
    func: Callable[[], T],
    logger,
    config: RetryConfig,
    retry_exceptions: Tuple[Type[BaseException], ...] = (Exception,),
) -> T:
    """
    Executa funcao com retry e backoff.
    Observacao: max_retries segue a semantica historica do projeto
    (permite tentativa final quando attempt == max_retries + 1).
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return func()
        except retry_exceptions as exc:
            retry_after = exc.retry_after if isinstance(exc, RetryableError) else None
            if attempt > config.max_retries:
                logger.exception(
                    "Retry | action=%s | falha definitiva apos %s tentativas: %s",
                    action_label,
                    attempt - 1,
                    type(exc).__name__,
                )
                raise

            sleep_s = backoff_seconds(config, attempt, retry_after=retry_after)
            logger.warning(
                "Retry | action=%s | tentativa=%s/%s | erro=%s | aguardando %.1fs",
                action_label,
                attempt,
                config.max_retries,
                type(exc).__name__,
                sleep_s,
            )
            time.sleep(sleep_s)
