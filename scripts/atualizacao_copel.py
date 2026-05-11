"""
Atualização dos relatórios Copel - Adaptado para servidor Railway.

Fluxo:
1. Baixa os .txt da pasta de ORIGEM no Google Drive
2. Baixa o output.xlsx existente do Drive (acumula histórico)
3. Processa os .txt (manipulação de colunas)
4. Busca dados de Usinas/Cooperados na ClickUp API (substitui FM10/FM11)
5. Faz merge, deduplica, ordena
6. Sobe os outputs para a pasta de DESTINO no Google Drive
"""

import os
import json
import glob
import logging
import tempfile
import time
from datetime import datetime
from io import BytesIO
from typing import Optional

import pandas as pd
pd.set_option("display.max_columns", None)

import locale
try:
    locale.setlocale(locale.LC_ALL, 'pt_BR.UTF-8')
except Exception:
    try:
        locale.setlocale(locale.LC_ALL, 'C.UTF-8')
    except Exception:
        pass

# Google APIs
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google.oauth2.credentials import Credentials as OAuthCredentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# ClickUp client
from scripts.clickup_client import (
    get_tasks_from_list,
    extract_custom_field,
    get_task_name_prefix,
    get_task_status,
)
from scripts.retry_utils import execute_with_retry, load_retry_config_from_env

logger = logging.getLogger("scheduler.atualizacao_copel")


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURAÇÃO (vem do .env via variáveis de ambiente)
# ══════════════════════════════════════════════════════════════════════════════

# ClickUp - Campos customizados
CF_UC = os.getenv("CF_UC", "abb7e1e9-3c99-4044-b20c-5eb19575a6d5")
CF_NOME_FANTASIA = os.getenv("CF_NOME_FANTASIA", "44468280-77b8-4bf1-8b20-416dcc752646")
CF_RAZAO_SOCIAL = os.getenv("CF_RAZAO_SOCIAL", "dfb0de9b-121a-4bf6-977f-dfb5eec523cb")

# ClickUp - Listas
LIST_USINAS = os.getenv("CLICKUP_LIST_USINAS", "")
LIST_ONGOING = os.getenv("CLICKUP_LIST_ONGOING", "")
LIST_PLANEJAMENTO_BLACK = os.getenv("CLICKUP_LIST_PLANEJAMENTO_BLACK", "")
LIST_HELEXIA_ONGOING = os.getenv("CLICKUP_LIST_HELEXIA_ONGOING", "")

# Google Drive - Pastas
GDRIVE_SOURCE_FOLDER_ID = os.getenv("GDRIVE_SOURCE_FOLDER_ID", "")
GDRIVE_OUTPUT_FOLDER_ID = os.getenv("GDRIVE_OUTPUT_FOLDER_ID", "")

# Diretório temporário local (não precisa de volume persistente)
def _resolve_data_dir(raw_value: str) -> str:
    """
    Resolve DATA_DIR de forma portável entre Linux/Windows.
    Em Windows, caminhos estilo /tmp podem apontar para raiz e causar erro de permissão.
    """
    configured = (raw_value or "").strip()
    if not configured:
        configured = "/tmp/copel_data"

    if os.name == "nt" and configured.startswith(("/tmp", "\\tmp")):
        fallback = os.path.join(tempfile.gettempdir(), "copel_data")
        logger.warning(
            "DATA_DIR '%s' incompatível com Windows. Usando '%s'.",
            configured,
            fallback,
        )
        return fallback

    return configured


DATA_DIR = _resolve_data_dir(os.getenv("DATA_DIR", "/tmp/copel_data"))
MEMORY_LOG_ENABLED = os.getenv("MEMORY_LOG_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
MEMORY_LOG_DEEP = os.getenv("MEMORY_LOG_DEEP", "0").strip().lower() in {"1", "true", "yes"}
GOOGLE_RETRY_CONFIG = load_retry_config_from_env("GOOGLE_API")


def _short_id(value: str, keep: int = 6) -> str:
    """Versao curta para IDs em logs."""
    if not value:
        return "(vazio)"
    text = str(value)
    if len(text) <= keep:
        return text
    return f"...{text[-keep:]}"


def _uc_set(series: pd.Series) -> set[str]:
    """Normaliza uma serie em conjunto de UCs sem vazios."""
    values = series.dropna().astype(str).str.strip()
    values = values[values != ""]
    return set(values.tolist())


def _read_proc_status_memory_mb() -> tuple[Optional[float], Optional[float]]:
    """Lê RSS e HWM de /proc/self/status (Linux), em MB."""
    status_path = "/proc/self/status"
    if not os.path.exists(status_path):
        return None, None

    rss_kib = None
    hwm_kib = None
    try:
        with open(status_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss_kib = int(line.split()[1])
                elif line.startswith("VmHWM:"):
                    hwm_kib = int(line.split()[1])
    except Exception:
        return None, None

    rss_mb = (rss_kib / 1024.0) if rss_kib is not None else None
    hwm_mb = (hwm_kib / 1024.0) if hwm_kib is not None else None
    return rss_mb, hwm_mb


def _get_ru_maxrss_mb() -> Optional[float]:
    """Retorna ru_maxrss em MB quando disponível (Unix)."""
    try:
        import resource  # type: ignore
    except Exception:
        return None

    try:
        ru_maxrss = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except Exception:
        return None

    # Linux reporta em KiB, macOS em bytes.
    if hasattr(os, "uname") and os.uname().sysname == "Darwin":
        return ru_maxrss / (1024.0 * 1024.0)
    return ru_maxrss / 1024.0


def _dataframe_mem_mb(df: Optional[pd.DataFrame]) -> Optional[float]:
    """Estimativa de memória de DataFrame em MB."""
    if df is None:
        return None
    try:
        return float(df.memory_usage(deep=MEMORY_LOG_DEEP).sum()) / (1024.0 * 1024.0)
    except Exception:
        return None


def log_memory_snapshot(stage: str, dataframes: Optional[dict[str, pd.DataFrame]] = None) -> None:
    """Registra snapshot de memória do processo e dos principais DataFrames."""
    if not MEMORY_LOG_ENABLED:
        return

    rss_mb, hwm_mb = _read_proc_status_memory_mb()
    ru_maxrss_mb = _get_ru_maxrss_mb()

    parts = [f"Memoria | stage={stage}"]
    if rss_mb is not None:
        parts.append(f"rss={rss_mb:.1f}MB")
    if hwm_mb is not None:
        parts.append(f"hwm={hwm_mb:.1f}MB")
    if ru_maxrss_mb is not None:
        parts.append(f"ru_maxrss={ru_maxrss_mb:.1f}MB")

    if dataframes:
        for name, df in dataframes.items():
            mem_mb = _dataframe_mem_mb(df)
            if mem_mb is None:
                continue
            parts.append(f"{name}_rows={len(df)}")
            parts.append(f"{name}_mem={mem_mb:.1f}MB")

    logger.info(" | ".join(parts))


# ══════════════════════════════════════════════════════════════════════════════
#  AUTENTICAÇÃO GOOGLE
# ══════════════════════════════════════════════════════════════════════════════

def get_google_credentials():
    """
    Cria credenciais:
    - Service Account (default) via GOOGLE_SERVICE_ACCOUNT_JSON
    - OAuth (modo teste) quando USE_OAUTH=1
    """
    scopes = [
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.file",
    ]

    use_oauth = os.getenv("USE_OAUTH", "").strip().lower() in {"1", "true", "yes"}
    if use_oauth:
        logger.info("Google Auth | modo OAuth habilitado")
        client_secret_path = os.getenv("OAUTH_CLIENT_SECRET_PATH", "client_secret.json")
        token_path = os.getenv("OAUTH_TOKEN_PATH", "token.json")

        creds = None
        if os.path.exists(token_path):
            logger.info("Google Auth | token OAuth encontrado em %s", token_path)
            creds = OAuthCredentials.from_authorized_user_file(token_path, scopes=scopes)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                logger.info("Google Auth | token ausente/invalido, iniciando fluxo OAuth")
                flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, scopes=scopes)
                # Abre navegador local para autenticação
                creds = flow.run_local_server(port=0)
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(creds.to_json())

        logger.info("Google Auth | credenciais OAuth prontas")
        return creds

    # Default: Service Account
    logger.info("Google Auth | modo Service Account")
    sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sa_json:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON não definida no .env")
    sa_info = json.loads(sa_json)
    logger.info("Google Auth | credenciais Service Account carregadas")
    return ServiceAccountCredentials.from_service_account_info(sa_info, scopes=scopes)


def get_drive_service():
    """Retorna instância autenticada do Google Drive API."""
    logger.info("Google Drive | conectando na API")
    creds = get_google_credentials()
    service = execute_with_retry(
        action_label="google_drive_build_service",
        func=lambda: build("drive", "v3", credentials=creds),
        logger=logger,
        config=GOOGLE_RETRY_CONFIG,
        retry_exceptions=(Exception,),
    )
    logger.info("Google Drive | conexao estabelecida")
    return service


# ══════════════════════════════════════════════════════════════════════════════
#  GOOGLE DRIVE - DOWNLOAD
# ══════════════════════════════════════════════════════════════════════════════

def download_txt_from_drive(drive_service, folder_id: str, dest_dir: str) -> int:
    """
    Baixa todos os arquivos .txt de uma pasta do Google Drive.
    Retorna a quantidade de arquivos baixados.
    """
    os.makedirs(dest_dir, exist_ok=True)
    logger.info(
        "Google Drive | listando .txt | folder=%s | destino=%s",
        _short_id(folder_id),
        dest_dir,
    )

    query = f"'{folder_id}' in parents and trashed=false and mimeType='text/plain'"
    try:
        results = execute_with_retry(
            action_label=f"drive_list_txt_folder_{_short_id(folder_id)}",
            func=lambda: drive_service.files().list(
                q=query,
                fields="files(id, name)",
                pageSize=500,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute(),
            logger=logger,
            config=GOOGLE_RETRY_CONFIG,
            retry_exceptions=(Exception,),
        )
    except Exception:
        logger.error("Google Drive | falha ao listar .txt da pasta %s", _short_id(folder_id))
        return 0
    files = results.get("files", [])

    if not files:
        logger.warning(f"Nenhum .txt encontrado na pasta do Drive ({folder_id})")
        return 0

    count = 0
    for f in files:
        file_id = f["id"]
        file_name = f["name"]
        if not file_name.lower().endswith(".txt"):
            continue

        try:
            content = execute_with_retry(
                action_label=f"drive_get_media_txt_{file_name}",
                func=lambda: drive_service.files().get_media(
                    fileId=file_id,
                    supportsAllDrives=True,
                ).execute(),
                logger=logger,
                config=GOOGLE_RETRY_CONFIG,
                retry_exceptions=(Exception,),
            )
        except Exception:
            logger.error("Google Drive | falha ao baixar .txt %s. Seguindo com os demais.", file_name)
            continue
        filepath = os.path.join(dest_dir, file_name)
        with open(filepath, "wb") as out:
            out.write(content)
        count += 1

    logger.info("Google Drive | download .txt concluido | arquivos=%s", count)
    return count


def download_file_from_drive(drive_service, folder_id: str, filename: str, dest_path: str) -> bool:
    """
    Baixa um arquivo específico de uma pasta do Drive.
    Retorna True se encontrou e baixou, False se não existe.
    """
    logger.info(
        "Google Drive | buscando arquivo | folder=%s | arquivo=%s",
        _short_id(folder_id),
        filename,
    )
    query = f"name='{filename}' and '{folder_id}' in parents and trashed=false"
    try:
        results = execute_with_retry(
            action_label=f"drive_find_file_{filename}",
            func=lambda: drive_service.files().list(
                q=query,
                fields="files(id)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute(),
            logger=logger,
            config=GOOGLE_RETRY_CONFIG,
            retry_exceptions=(Exception,),
        )
    except Exception:
        logger.error("Google Drive | nao foi possivel localizar arquivo %s no Drive.", filename)
        return False
    files = results.get("files", [])

    if not files:
        return False

    file_id = files[0]["id"]
    try:
        content = execute_with_retry(
            action_label=f"drive_download_file_{filename}",
            func=lambda: drive_service.files().get_media(
                fileId=file_id,
                supportsAllDrives=True,
            ).execute(),
            logger=logger,
            config=GOOGLE_RETRY_CONFIG,
            retry_exceptions=(Exception,),
        )
    except Exception:
        logger.error("Google Drive | falha ao baixar arquivo %s.", filename)
        return False

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "wb") as out:
        out.write(content)

    logger.info(f"  Baixado do Drive: {filename}")
    return True

# ──────────────────────────────────────────────────────────────────────────────
# GOOGLE DRIVE - RESOLVER PASTA MENSAL
# ──────────────────────────────────────────────────────────────────────────────

def get_drive_folder_id_by_name(drive_service, parent_id: str, folder_name: str) -> Optional[str]:
    """
    Busca uma subpasta pelo nome dentro de um folder pai no Google Drive.
    Retorna o ID se encontrar, ou None.
    """
    query = (
        f"name='{folder_name}' and '{parent_id}' in parents and trashed=false "
        "and mimeType='application/vnd.google-apps.folder'"
    )
    results = execute_with_retry(
        action_label=f"drive_find_folder_{folder_name}",
        func=lambda: drive_service.files().list(
            q=query,
            fields="files(id, name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute(),
        logger=logger,
        config=GOOGLE_RETRY_CONFIG,
        retry_exceptions=(Exception,),
    )
    files = results.get("files", [])
    if not files:
        return None
    return files[0]["id"]


def resolve_monthly_source_folder_id(drive_service, parent_id: str, ref_date: datetime) -> Optional[str]:
    """
    No Drive, a pasta de origem agora é organizada assim:
    Pasta Pai -> subpastas mensais no formato MM-AAAA (ex: 01-2026) -> arquivos .txt

    Se a subpasta do mês existir, usa ela.
    Se não existir, retorna None (sem fallback para pasta pai).
    """
    folder_name = f"{ref_date.month:02}-{ref_date.year:04}"
    try:
        child_id = get_drive_folder_id_by_name(drive_service, parent_id, folder_name)
    except Exception:
        logger.error(
            "Google Drive | falha ao resolver pasta mensal %s.",
            folder_name,
        )
        return None
    if child_id:
        logger.info(f"Pasta mensal encontrada no Drive: {folder_name}")
        return child_id

    logger.error("Pasta mensal '%s' não encontrada na origem.", folder_name)
    return None

def parse_month_override(value: str) -> Optional[datetime]:
    """
    Aceita formatos:
      - MM-AAAA (ex: 01-2026)
      - AAAA-MM (ex: 2026-01)
      - MM/AAAA (ex: 01/2026)
    Retorna um datetime no 1º dia do mês ou None se inválido.
    """
    if not value:
        return None

    v = value.strip()
    for fmt in ("%m-%Y", "%Y-%m", "%m/%Y"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None

# ══════════════════════════════════════════════════════════════════════════════
#  GOOGLE DRIVE - UPLOAD
# ══════════════════════════════════════════════════════════════════════════════

def upload_to_drive(drive_service, filepath, filename, folder_id,
                    mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"):
    """Faz upload ou atualiza arquivo no Google Drive."""
    if not folder_id:
        logger.warning(f"Pasta de destino não definida. Pulando upload de {filename}.")
        return

    logger.info(
        "Google Drive | upload iniciado | folder=%s | arquivo=%s",
        _short_id(folder_id),
        filename,
    )
    query = f"name='{filename}' and '{folder_id}' in parents and trashed=false"
    try:
        results = execute_with_retry(
            action_label=f"drive_find_upload_target_{filename}",
            func=lambda: drive_service.files().list(
                q=query,
                fields="files(id)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute(),
            logger=logger,
            config=GOOGLE_RETRY_CONFIG,
            retry_exceptions=(Exception,),
        )
    except Exception:
        logger.error("Google Drive | falha ao consultar destino de upload para %s", filename)
        return
    existing = results.get("files", [])

    with open(filepath, "rb") as f:
        media = MediaIoBaseUpload(BytesIO(f.read()), mimetype=mime_type, resumable=True)

    if existing:
        file_id = existing[0]["id"]
        execute_with_retry(
            action_label=f"drive_update_file_{filename}",
            func=lambda: drive_service.files().update(
                fileId=file_id,
                media_body=media,
                supportsAllDrives=True,
            ).execute(),
            logger=logger,
            config=GOOGLE_RETRY_CONFIG,
            retry_exceptions=(Exception,),
        )
        logger.info(f"  Atualizado no Drive: {filename}")
    else:
        file_metadata = {"name": filename, "parents": [folder_id]}
        execute_with_retry(
            action_label=f"drive_create_file_{filename}",
            func=lambda: drive_service.files().create(
                body=file_metadata,
                media_body=media,
                supportsAllDrives=True,
            ).execute(),
            logger=logger,
            config=GOOGLE_RETRY_CONFIG,
            retry_exceptions=(Exception,),
        )
        logger.info(f"  Criado no Drive: {filename}")


# ══════════════════════════════════════════════════════════════════════════════
#  CLICKUP → DataFrames (substitui FM10 e FM11)
# ══════════════════════════════════════════════════════════════════════════════

def carregar_fm11_clickup() -> pd.DataFrame:
    """
    Equivalente à FM11 (Geração/Cadastro).
    Puxa da lista de Usinas.
    Colunas: ['Usina', 'Status Usina', 'UC']
    """
    logger.info("Carregando FM11 do ClickUp (Lista Usinas)...")
    tasks = get_tasks_from_list(LIST_USINAS, include_closed=True)
    logger.info(f"  {len(tasks)} tasks encontradas")

    rows = []
    for task in tasks:
        usina = get_task_name_prefix(task, separator=" - ")
        status = get_task_status(task)
        uc = extract_custom_field(task, CF_UC)
        if uc:
            rows.append({"Usina": usina, "Status Usina": status, "UC": str(uc).strip()})

    df = pd.DataFrame(rows, columns=["Usina", "Status Usina", "UC"])
    df = df.drop_duplicates(subset=["UC"], keep="last")

    # Entrada hardcoded mantida
    df.loc[len(df)] = ["UFV Helexia 2", "Ativo", "111113431"]

    logger.info(f"  FM11 final: {len(df)} registros")
    return df


def carregar_fm10_clickup() -> pd.DataFrame:
    """
    Equivalente à FM10 (Dados Cadastrais).
    Puxa de 3 listas: Ongoing, Planejamento Black, Helexia Ongoing.
    Colunas: ['UC', 'Nome Fantasia', 'Razão Social (Matriz) /Pessoa']
    """
    logger.info("Carregando FM10 do ClickUp (3 listas)...")

    listas = {
        "Ongoing": LIST_ONGOING,
        "Planejamento Black": LIST_PLANEJAMENTO_BLACK,
        "Helexia Ongoing": LIST_HELEXIA_ONGOING,
    }

    all_tasks = []
    for nome, list_id in listas.items():
        if not list_id:
            logger.warning(f"  Lista {nome} sem ID configurado, pulando.")
            continue
        tasks = get_tasks_from_list(list_id, include_closed=True)
        logger.info(f"  {len(tasks)} tasks de {nome}")
        all_tasks.extend(tasks)

    rows = []
    for task in all_tasks:
        uc = extract_custom_field(task, CF_UC)
        nome_fantasia = extract_custom_field(task, CF_NOME_FANTASIA)
        razao_social = extract_custom_field(task, CF_RAZAO_SOCIAL)
        if uc:
            rows.append({
                "UC": str(uc).strip(),
                "Nome Fantasia": nome_fantasia,
                "Razão Social (Matriz) /Pessoa": razao_social,
            })

    df = pd.DataFrame(rows, columns=["UC", "Nome Fantasia", "Razão Social (Matriz) /Pessoa"])
    df = df.drop_duplicates(subset=["UC"], keep="last")

    logger.info(f"  FM10 final: {len(df)} registros")
    return df


# ══════════════════════════════════════════════════════════════════════════════
#  FUNÇÕES DE MANIPULAÇÃO DE COLUNAS (mantidas do original)
# ══════════════════════════════════════════════════════════════════════════════

def manip_28(df):
    df.loc[:,('Saldo anterior Ponta')] = df.loc[:,('Saldo anterior Ponta')].astype(int)
    df.loc[:,('Saldo anterior Fora Ponta')] = df.loc[:,('Saldo anterior Fora Ponta')].astype(int)
    df.loc[:,('Saldo anterior TP')] = df.loc[:,('Saldo anterior TP')].astype(int)
    df.loc[:,('Crédito recebido outra UC Fora Ponta')] = df.loc[:,('Crédito recebido outra UC Fora Ponta')].astype(int)
    df.loc[:,('Crédito recebido outra UC Ponta')] = df.loc[:,('Crédito recebido outra UC Ponta')].astype(int)
    df.loc[:,('Credito recebido outra UC TP')] = df.loc[:,('Credito recebido outra UC TP')].astype(int)
    df.loc[:,('Energia Injetada Ponta')] = df.loc[:,('Energia Injetada Ponta')].astype(int)
    df.loc[:,('Energia Injetada Fora Ponta')] = df.loc[:,('Energia Injetada Fora Ponta')].astype(int)
    df.loc[:,('Energia Injetada TP')] = df.loc[:,('Energia Injetada TP')].astype(int)
    df.loc[:,('Energia Ativa Ponta')] = df.loc[:,('Energia Ativa Ponta')].astype(int)
    df.loc[:,('Energia Ativa Fora Ponta')] = df.loc[:,('Energia Ativa Fora Ponta')].astype(int)
    df.loc[:,('Energia Ativa TP')] = df.loc[:,('Energia Ativa TP')].astype(int)
    df.loc[:,('Crédito Utilizado no Mês Ponta')] = df.loc[:,('Crédito Utilizado no Mês Ponta')].astype(int)
    df.loc[:,('Crédito Utilizado no Mês Fora Ponta')] = df.loc[:,('Crédito Utilizado no Mês Fora Ponta')].astype(int)
    df.loc[:,('Crédito Utilizado no Mês TP')] = df.loc[:,('Crédito Utilizado no Mês TP')].astype(int)
    df.loc[:,('Percentual')] = df.loc[:,('Percentual')].astype(float)
    df.loc[:,('Prioridade')] = df.loc[:,('Prioridade')].astype(int)
    df.loc[:,('Saldo Transferido Outra UC Ponta')] = df.loc[:,('Saldo Transferido Outra UC Ponta')].astype(int)
    df.loc[:,('Saldo Transferido Outra UC Fora Ponta')] = df.loc[:,('Saldo Transferido Outra UC Fora Ponta')].astype(int)
    df.loc[:,('Saldo Transferido Outra UC TP')] = df.loc[:,('Saldo Transferido Outra UC TP')].astype(int)
    df.loc[:,('Saldo Final Ponta')] = df.loc[:,('Saldo Final Ponta')].astype(int)
    df.loc[:,('Saldo Final Fora Ponta')] = df.loc[:,('Saldo Final Fora Ponta')].astype(int)
    df.loc[:,('Saldo Final TP')] = df.loc[:,('Saldo Final TP')].astype(int)


def manip_21_usina(df):
    df.loc[:,('Saldo Final Fora Ponta')] = df.loc[:,('Percentual')].astype(int)
    df.loc[:,('Percentual')] = df.loc[:,('Energia Ativa Fora Ponta')].astype(float)
    df.loc[:,('Energia Ativa Fora Ponta')] = df.loc[:,('Energia Injetada Fora Ponta')].astype(int)
    df.loc[:,('Energia Injetada Fora Ponta')] = df.loc[:,('Credito recebido outra UC TP')].astype(int)
    df.loc[:,('Credito recebido outra UC TP')] = 0
    df.loc[:,('Saldo Transferido Outra UC Fora Ponta')] = df.loc[:,('Crédito Utilizado no Mês Fora Ponta')].astype(int)
    df.loc[:,('Crédito Utilizado no Mês Fora Ponta')] = df.loc[:,('Energia Ativa Ponta')].astype(int)
    df.loc[:,('Energia Ativa Ponta')] = df.loc[:,('Energia Injetada Ponta')].astype(int)
    df.loc[:,('Energia Injetada Ponta')] = df.loc[:,('Crédito recebido outra UC Fora Ponta')].astype(int)
    df.loc[:,('Crédito recebido outra UC Fora Ponta')] = df.loc[:,('Crédito recebido outra UC Ponta')].astype(int)
    df.loc[:,('Crédito recebido outra UC Ponta')] = df.loc[:,('Saldo anterior TP')].astype(int)
    df.loc[:,('Saldo anterior TP')] = 0
    df.loc[:,('Saldo Transferido Outra UC Ponta')] = df.loc[:,('Crédito Utilizado no Mês Ponta')].astype(int)
    df.loc[:,('Crédito Utilizado no Mês Ponta')] = df.loc[:,('Energia Injetada TP')].astype(int)
    df.loc[:,('Energia Injetada TP')] = 0
    df.loc[:,('Prioridade')] = df.loc[:,('Energia Ativa TP')].astype(int)
    df.loc[:,('Energia Ativa TP')] = 0
    df.loc[:,('Saldo Final Ponta')] = df.loc[:,('Crédito Utilizado no Mês TP')].astype(int)
    df.loc[:,('Saldo Transferido Outra UC TP')] = 0
    df.loc[:,('Crédito Utilizado no Mês TP')] = 0
    df.loc[:,('Saldo Final TP')] = 0
    df.loc[:,('Saldo anterior Fora Ponta')] = df.loc[:,('Saldo anterior Fora Ponta')].astype(int)
    df.loc[:,('Saldo anterior Ponta')] = df.loc[:,('Saldo anterior Ponta')].astype(int)


def manip_21_coop(df):
    df.loc[:,('Saldo Final TP')] = df.loc[:,('Percentual')].astype(int)
    df.loc[:,('Percentual')] = df.loc[:,('Energia Ativa Fora Ponta')].astype(float)
    df.loc[:,('Energia Ativa Fora Ponta')] = df.loc[:,('Energia Injetada Ponta')].astype(int)
    df.loc[:,('Energia Injetada Ponta')] = 0
    df.loc[:,('Saldo Final Fora Ponta')] = df.loc[:,('Crédito Utilizado no Mês TP')].astype(int)
    df.loc[:,('Crédito Utilizado no Mês TP')] = df.loc[:,('Energia Ativa Ponta')].astype(int)
    df.loc[:,('Energia Ativa Ponta')] = 0
    df.loc[:,('Saldo Final Ponta')] = 0
    df.loc[:,('Saldo Transferido Outra UC TP')] = df.loc[:,('Crédito Utilizado no Mês Fora Ponta')].astype(int)
    df.loc[:,('Crédito Utilizado no Mês Fora Ponta')] = df.loc[:,('Energia Injetada TP')].astype(int)
    df.loc[:,('Energia Injetada TP')] = df.loc[:,('Credito recebido outra UC TP')].astype(int)
    df.loc[:,('Credito recebido outra UC TP')] = df.loc[:,('Crédito recebido outra UC Ponta')].astype(int)
    df.loc[:,('Crédito recebido outra UC Ponta')] = 0
    df.loc[:,('Saldo Transferido Outra UC Fora Ponta')] = df.loc[:,('Crédito Utilizado no Mês Ponta')].astype(int)
    df.loc[:,('Crédito Utilizado no Mês Ponta')] = 0
    df.loc[:,('Saldo Transferido Outra UC Ponta')] = 0
    df.loc[:,('Prioridade')] = df.loc[:,('Energia Ativa TP')].astype(int)
    df.loc[:,('Energia Ativa TP')] = df.loc[:,('Energia Injetada Fora Ponta')].astype(int)
    df.loc[:,('Energia Injetada Fora Ponta')] = df.loc[:,('Crédito recebido outra UC Fora Ponta')].astype(int)
    df.loc[:,('Crédito recebido outra UC Fora Ponta')] = df.loc[:,('Saldo anterior TP')].astype(int)
    df.loc[:,('Saldo anterior TP')] = df.loc[:,('Saldo anterior Fora Ponta')].astype(int)
    df.loc[:,('Saldo anterior Fora Ponta')] = df.loc[:,('Saldo anterior Ponta')].astype(int)
    df.loc[:,('Saldo anterior Ponta')] = 0


def manip_14(df):
    df.loc[:,('Saldo Final TP')] = df.loc[:,('Energia Injetada TP')].astype(int)
    df.loc[:,('Energia Injetada TP')] = df.loc[:,('Saldo anterior TP')].astype(int)
    df.loc[:,('Saldo anterior TP')] = df.loc[:,('Saldo anterior Ponta')].astype(int)
    df.loc[:,('Saldo anterior Ponta')] = 0
    df.loc[:,('Saldo Final Fora Ponta')] = 0
    df.loc[:,('Saldo Final Ponta')] = 0
    df.loc[:,('Saldo Transferido Outra UC TP')] = df.loc[:,('Energia Injetada Fora Ponta')].astype(int)
    df.loc[:,('Energia Injetada Fora Ponta')] = 0
    df.loc[:,('Saldo Transferido Outra UC Fora Ponta')] = 0
    df.loc[:,('Saldo Transferido Outra UC Ponta')] = 0
    df.loc[:,('Prioridade')] = df.loc[:,('Energia Injetada Ponta')].astype(int)
    df.loc[:,('Energia Injetada Ponta')] = 0
    df.loc[:,('Percentual')] = df.loc[:,('Credito recebido outra UC TP')].astype(float)
    df.loc[:,('Credito recebido outra UC TP')] = df.loc[:,('Saldo anterior Fora Ponta')].astype(int)
    df.loc[:,('Saldo anterior Fora Ponta')] = 0
    df.loc[:,('Crédito Utilizado no Mês TP')] = df.loc[:,('Crédito recebido outra UC Fora Ponta')].astype(int)
    df.loc[:,('Crédito recebido outra UC Fora Ponta')] = 0
    df.loc[:,('Crédito Utilizado no Mês Fora Ponta')] = 0
    df.loc[:,('Crédito Utilizado no Mês Ponta')] = 0
    df.loc[:,('Energia Ativa TP')] = df.loc[:,('Crédito recebido outra UC Ponta')].astype(int)
    df.loc[:,('Crédito recebido outra UC Ponta')] = 0
    df.loc[:,('Energia Ativa Fora Ponta')] = 0
    df.loc[:,('Energia Ativa Ponta')] = 0


def calcular_ucs_nao_encontradas(df_origem: pd.DataFrame, df_fm11: pd.DataFrame, df_fm10: pd.DataFrame) -> dict:
    """
    Calcula UCs sem correspondencia nos cadastros:
    - usinas: UC Ger. ausente no FM11
    - cooperados: UC (apenas linhas cooperadas) ausente no FM10
    """
    origem_uc_ger = _uc_set(df_origem["UC Ger."])
    fm11_uc = _uc_set(df_fm11["UC"])
    missing_usinas = sorted(origem_uc_ger - fm11_uc)

    coop_mask = df_origem["UC Ger."].astype(str).str.strip() != df_origem["UC"].astype(str).str.strip()
    origem_uc_coop = _uc_set(df_origem.loc[coop_mask, "UC"])
    fm10_uc = _uc_set(df_fm10["UC"])
    missing_cooperados = sorted(origem_uc_coop - fm10_uc)

    return {
        "usinas": missing_usinas,
        "cooperados": missing_cooperados,
    }


def log_ucs_nao_encontradas(ucs_nao_encontradas: dict) -> None:
    """Registra resumo final com UCs nao encontradas para usinas e cooperados."""
    missing_usinas = ucs_nao_encontradas.get("usinas", [])
    missing_cooperados = ucs_nao_encontradas.get("cooperados", [])

    if missing_usinas:
        logger.warning(
            "UCs nao encontradas - usinas (FM11) | total=%s | lista=%s",
            len(missing_usinas),
            ", ".join(missing_usinas),
        )
    else:
        logger.info("UCs nao encontradas - usinas (FM11) | total=0")

    if missing_cooperados:
        logger.warning(
            "UCs nao encontradas - cooperados (FM10) | total=%s | lista=%s",
            len(missing_cooperados),
            ", ".join(missing_cooperados),
        )
    else:
        logger.info("UCs nao encontradas - cooperados (FM10) | total=0")


def build_abort_result(ucs_nao_encontradas: dict, reason: str) -> dict:
    """
    Retorna payload padrao para sinalizar abortamento sem excecao.
    """
    result = dict(ucs_nao_encontradas)
    result["_aborted"] = True
    result["_abort_reason"] = reason
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  FUNÇÃO PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════════

def tarefa_atualizacao_copel():
    """
    Tarefa principal de atualizacao dos relatorios Copel.
    Retorna dict com listas de UCs nao encontradas.
    """
    started_total = time.perf_counter()
    ucs_nao_encontradas = {"usinas": [], "cooperados": []}

    hoje = datetime.now()
    ref_date = hoje
    month_override = os.getenv("SOURCE_MONTH", "").strip()
    if month_override:
        parsed = parse_month_override(month_override)
        if parsed:
            ref_date = parsed
        else:
            logger.warning(
                f"SOURCE_MONTH inválido: {month_override}. Usando mês atual."
            )

    ano_atual = f"{ref_date.year:04}"
    mes_atual = f"{ref_date.month:02}"
    logger.info(
        "Processo | inicio atualizacao copel | referencia=%s-%s",
        mes_atual,
        ano_atual,
    )
    log_memory_snapshot("inicio")

    # ── Diretórios temporários locais ─────────────────────────────────────────
    relatorios_dir = os.path.join(DATA_DIR, "Relatorios")
    output_path = os.path.join(DATA_DIR, "output.xlsx")

    os.makedirs(relatorios_dir, exist_ok=True)

    logger.info("Processo | limpando relatorios temporarios antigos")
    for filename in os.listdir(relatorios_dir):
        file_path = os.path.join(relatorios_dir, filename)
        if os.path.isfile(file_path):
            try:
                os.remove(file_path)
            except PermissionError:
                logger.warning(f"Arquivo em uso, não removido: {file_path}")
    log_memory_snapshot("apos_limpeza_inicial")

    # ── Google Drive — downloads ──────────────────────────────────────────────
    drive_service = get_drive_service()

    if not GDRIVE_SOURCE_FOLDER_ID:
        logger.error("GDRIVE_SOURCE_FOLDER_ID não definido. Impossível continuar.")
        return build_abort_result(
            ucs_nao_encontradas,
            "GDRIVE_SOURCE_FOLDER_ID nao definido",
        )

    source_folder_id = resolve_monthly_source_folder_id(
        drive_service,
        GDRIVE_SOURCE_FOLDER_ID,
        ref_date,
    )
    if not source_folder_id:
        logger.error("Pasta mensal de origem obrigatória não encontrada. Abortando.")
        return build_abort_result(
            ucs_nao_encontradas,
            "pasta mensal de origem nao encontrada",
        )

    logger.info(
        "Processo | baixando relatorios txt | source_folder=%s",
        _short_id(source_folder_id),
    )
    txt_count = download_txt_from_drive(drive_service, source_folder_id, relatorios_dir)
    if txt_count == 0:
        logger.warning("Nenhum .txt encontrado na pasta de origem. Abortando.")
        return build_abort_result(
            ucs_nao_encontradas,
            "nenhum arquivo txt encontrado na pasta mensal",
        )
    log_memory_snapshot("apos_download_txt")

    if GDRIVE_OUTPUT_FOLDER_ID:
        logger.info("Processo | baixando output.xlsx existente para merge de historico")
        download_file_from_drive(drive_service, GDRIVE_OUTPUT_FOLDER_ID, "output.xlsx", output_path)
    log_memory_snapshot("apos_download_output_existente")

    # ── Carregar dados do ClickUp (substitui FM10/FM11) ───────────────────────
    logger.info("Processo | carregando dados de usinas/cooperados no ClickUp")
    clickup_start = time.perf_counter()
    df_fm11 = carregar_fm11_clickup()
    df_fm10 = carregar_fm10_clickup()
    logger.info(
        "Processo | ClickUp concluido | fm11=%s | fm10=%s | %.2fs",
        len(df_fm11),
        len(df_fm10),
        time.perf_counter() - clickup_start,
    )
    log_memory_snapshot("apos_clickup", {"df_fm11": df_fm11, "df_fm10": df_fm10})

    # ── Cabeçalho dos relatórios .txt ─────────────────────────────────────────
    first_line = [
        'Vazio1', 'UC Ger.', 'Cliente Ger.', 'Vazio2', 'UC', 'Cliente',
        'Mês Ref.', 'Saldo anterior Ponta', 'Saldo anterior Fora Ponta',
        'Saldo anterior TP', 'Crédito recebido outra UC Ponta',
        'Crédito recebido outra UC Fora Ponta', 'Credito recebido outra UC TP',
        'Energia Injetada Ponta', 'Energia Injetada Fora Ponta',
        'Energia Injetada TP', 'Energia Ativa Ponta', 'Energia Ativa Fora Ponta',
        'Energia Ativa TP', 'Crédito Utilizado no Mês Ponta',
        'Crédito Utilizado no Mês Fora Ponta', 'Crédito Utilizado no Mês TP',
        'Percentual', 'Prioridade', 'Saldo Transferido Outra UC Ponta',
        'Saldo Transferido Outra UC Fora Ponta', 'Saldo Transferido Outra UC TP',
        'Saldo Final Ponta', 'Saldo Final Fora Ponta', 'Saldo Final TP'
    ]

    # ── Leitura e concatenação dos .txt ───────────────────────────────────────
    allfiles = glob.glob(os.path.join(relatorios_dir, '*.txt'))
    logger.info("Processo | iniciando leitura de %s arquivo(s) .txt", len(allfiles))

    stage_start = time.perf_counter()
    df = pd.concat((
        pd.read_csv(
            f,
            delimiter=";",
            names=first_line,
            encoding="latin-1",
            engine="python",
            # Mantém colunas textuais como object para permitir trocas de dtype
            # durante as manipulações com .loc (int/float em colunas inicialmente texto).
            dtype=object,
        )
        for f in allfiles
    ))
    logger.info(
        "Processo | leitura dos .txt concluida | linhas=%s | %.2fs",
        len(df),
        time.perf_counter() - stage_start,
    )
    log_memory_snapshot("apos_leitura_txt", {"df_txt_bruto": df})

    # Remover linhas de cabeçalho (a cada 14 linhas)
    N = 14
    to_drop = [i for i in range(0, len(df), N)]
    if 0 not in to_drop:
        to_drop.insert(0, 0)
    df = df[~df.index.isin(to_drop)]

    df['Mês Ref.'] = pd.to_datetime(df['Mês Ref.'], format='%d/%m/%Y')
    df = df.drop(columns=['Vazio1', 'Vazio2'])

    # ── Aplicação das funções de manipulação ──────────────────────────────────
    logger.info("Processo | aplicando transformacoes de colunas")
    stage_start = time.perf_counter()
    rng_max = df.shape[0]
    count = 0
    blocos_processados = 0
    df2 = pd.DataFrame()
    while count < rng_max:
        df3 = df.iloc[count:count+13]
        ncols = len(df3.dropna(axis=1).columns)

        if ncols == 21 and df3.iloc[0:1]['UC Ger.'].iat[0] == df3.iloc[0:1]['UC'].iat[0]:
            manip_21_usina(df3)
        elif ncols == 21 and df3.iloc[0:1]['UC Ger.'].iat[0] != df3.iloc[0:1]['UC'].iat[0]:
            manip_21_coop(df3)
        elif ncols == 14:
            manip_14(df3)
        elif ncols < 14:
            pass
        else:
            manip_28(df3)

        df2 = pd.concat([df2, df3])
        count += 13
        blocos_processados += 1

    df2 = df2.dropna()
    logger.info(
        "Processo | transformacoes concluidas | blocos=%s | linhas=%s | %.2fs",
        blocos_processados,
        len(df2),
        time.perf_counter() - stage_start,
    )
    log_memory_snapshot("apos_transformacoes", {"df2": df2})

    # ── Diagnóstico de UCs não encontradas ────────────────────────────────────
    ucs_nao_encontradas = calcular_ucs_nao_encontradas(df2, df_fm11, df_fm10)

    # ── Merge com dados do ClickUp ────────────────────────────────────────────
    logger.info("Processo | realizando merges com FM11/FM10")
    df2 = df2.merge(df_fm11, how='inner', left_on='UC Ger.', right_on='UC', suffixes=('', '_y'))
    df2['Cliente Ger.'] = df2['Usina']
    df2 = df2.rename(columns={"Cliente Ger.": "Usinas"})

    df2 = df2.merge(df_fm10, how='left', on='UC')
    df2 = df2.drop(['Status Usina', 'UC_y', 'Usina'], axis=1)
    df2 = df2.iloc[:, [0, 1, 2, 28, 29, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27]]

    df2['UC Ger.'] = df2['UC Ger.'].astype(str)
    df2['UC'] = df2['UC'].astype(str)
    df2['Cliente'] = df2['Cliente'].astype(str)

    int_cols = [
        'Saldo anterior Ponta', 'Saldo anterior Fora Ponta', 'Saldo anterior TP',
        'Crédito recebido outra UC Ponta', 'Crédito recebido outra UC Fora Ponta',
        'Credito recebido outra UC TP', 'Energia Injetada Ponta',
        'Energia Injetada Fora Ponta', 'Energia Injetada TP',
        'Energia Ativa Ponta', 'Energia Ativa Fora Ponta', 'Energia Ativa TP',
        'Crédito Utilizado no Mês Ponta', 'Crédito Utilizado no Mês Fora Ponta',
        'Crédito Utilizado no Mês TP', 'Prioridade',
        'Saldo Transferido Outra UC Ponta', 'Saldo Transferido Outra UC Fora Ponta',
        'Saldo Transferido Outra UC TP', 'Saldo Final Ponta',
        'Saldo Final Fora Ponta', 'Saldo Final TP'
    ]
    df2 = df2.astype({col: 'int64' for col in int_cols})
    log_memory_snapshot("apos_merges_e_cast", {"df2": df2})

    # ── Juntar com output existente (baixado do Drive) ────────────────────────
    if os.path.exists(output_path):
        logger.info("Processo | mesclando com output.xlsx existente")
        df_xls = pd.read_excel(output_path)
        df_xls['UC Ger.'] = df_xls['UC Ger.'].astype(str)
        df_xls['UC'] = df_xls['UC'].astype(str)
        df_xls['Cliente'] = df_xls['Cliente'].astype(str)
        df2 = pd.concat([df2, df_xls])
        log_memory_snapshot("apos_concat_historico", {"df2": df2, "df_xls": df_xls})
    else:
        logger.info("Processo | output.xlsx inexistente, sera criado novo arquivo")

    # Remove duplicatas e ordena
    df2 = df2.drop_duplicates(subset=('UC Ger.', 'UC', 'Cliente', 'Mês Ref.'))
    df2 = df2.sort_values(by=['UC Ger.', 'Nome Fantasia', 'Mês Ref.'], ascending=[True, True, False])

    # ── Salvar localmente ─────────────────────────────────────────────────────
    df2.to_excel(output_path, index=False)
    logger.info(f"Salvo localmente: {output_path}")
    log_memory_snapshot("apos_salvar_output", {"df2": df2})

    # ── Separar usina e cooperado ─────────────────────────────────────────────
    df_usina = df2.loc[df2['UC Ger.'] == df2['UC']].drop(
        ['Nome Fantasia', 'Razão Social (Matriz) /Pessoa'], axis=1
    )
    df_coop = df2.loc[df2['UC Ger.'] != df2['UC']]

    output_usina = os.path.join(DATA_DIR, "output_usina.xlsx")
    output_coop = os.path.join(DATA_DIR, "output_coop.xlsx")
    output_mensal = os.path.join(DATA_DIR, f"output {mes_atual}_{ano_atual}.xlsx")

    logger.info("Processo | gerando arquivos de saida (usina, coop e consolidado mensal)")
    df_usina.to_excel(output_usina, index=False)
    df_coop.to_excel(output_coop, index=False)
    df2.to_excel(output_mensal, index=False)
    log_memory_snapshot("apos_gerar_arquivos", {"df_usina": df_usina, "df_coop": df_coop, "df2": df2})

    # ── Upload para Google Drive (pasta de destino) ───────────────────────────
    skip_upload = os.getenv("SKIP_UPLOAD", "").strip().lower() in {"1", "true", "yes"}
    if skip_upload:
        logger.warning("SKIP_UPLOAD ativo. Upload para Drive ignorado.")
    elif GDRIVE_OUTPUT_FOLDER_ID:
        logger.info("Processo | enviando outputs para o Google Drive")
        upload_to_drive(drive_service, output_path, "output.xlsx", GDRIVE_OUTPUT_FOLDER_ID)
        upload_to_drive(drive_service, output_mensal, f"output {mes_atual}_{ano_atual}.xlsx", GDRIVE_OUTPUT_FOLDER_ID)
        upload_to_drive(drive_service, output_usina, "output_usina.xlsx", GDRIVE_OUTPUT_FOLDER_ID)
        upload_to_drive(drive_service, output_coop, "output_coop.xlsx", GDRIVE_OUTPUT_FOLDER_ID)
    else:
        logger.warning("GDRIVE_OUTPUT_FOLDER_ID não definido. Upload ignorado.")

    # ── Limpeza dos arquivos temporários ──────────────────────────────────────
    logger.info("Processo | limpando arquivos temporarios")
    for f in glob.glob(os.path.join(DATA_DIR, "*")):
        if os.path.isfile(f):
            try:
                os.remove(f)
            except PermissionError:
                logger.warning(f"Arquivo em uso, não removido: {f}")
    for f in glob.glob(os.path.join(relatorios_dir, "*")):
        if os.path.isfile(f):
            try:
                os.remove(f)
            except PermissionError:
                logger.warning(f"Arquivo em uso, não removido: {f}")
    log_memory_snapshot("apos_limpeza_final")

    log_ucs_nao_encontradas(ucs_nao_encontradas)
    logger.info(
        "Atualização Copel concluída com sucesso! | tempo_total=%.2fs",
        time.perf_counter() - started_total,
    )
    ucs_nao_encontradas["_aborted"] = False
    return ucs_nao_encontradas
