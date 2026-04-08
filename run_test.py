"""
Carrega variaveis do .env.test e roda o scheduler.
Uso: python run_test.py
"""

from pathlib import Path
import logging
import os


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        raise FileNotFoundError(f"Arquivo .env.test nao encontrado em {env_path}")

    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip()


def configure_logging() -> None:
    log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )


def print_missing_uc_summary(result) -> None:
    if not isinstance(result, dict):
        return

    missing_usinas = result.get("usinas", [])
    missing_cooperados = result.get("cooperados", [])

    print("\nResumo final - UCs nao encontradas")
    print(
        "Usinas (FM11): "
        + (", ".join(missing_usinas) if missing_usinas else "nenhuma")
    )
    print(
        "Cooperados (FM10): "
        + (", ".join(missing_cooperados) if missing_cooperados else "nenhuma")
    )


def main() -> int:
    env_path = Path(__file__).resolve().parent / ".env.test"
    try:
        load_env_file(env_path)
    except FileNotFoundError as exc:
        print(str(exc))
        return 1

    configure_logging()
    logger = logging.getLogger("scheduler.run_test")

    print("Variaveis de teste carregadas de .env.test")
    logger.info("Execucao de teste iniciada")

    # Roda diretamente a tarefa da Copel (sem scheduler)
    from scripts.atualizacao_copel import tarefa_atualizacao_copel

    result = tarefa_atualizacao_copel()
    print_missing_uc_summary(result)
    logger.info("Execucao de teste finalizada")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
