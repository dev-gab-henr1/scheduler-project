# Scheduler Central

Serviço que roda o script da Copel em horário programado usando APScheduler.

## Estrutura

```
scheduler-project/
├── main.py                          # Scheduler principal
├── scripts/
│   ├── __init__.py
│   ├── atualizacao_copel.py         # Relatórios Copel → todo dia às 07:00
├── .env                             # Variáveis de ambiente (NÃO commitar)
├── .gitignore
├── requirements.txt
└── Procfile                         # Config Railway (worker)
```

## Setup da Service Account do Google

O script original usava OAuth (client_secret.json + token.json), que exige
navegador para autenticar. No servidor, usamos **Service Account**:

1. Acesse o [Google Cloud Console](https://console.cloud.google.com/)
2. Crie um projeto (ou use existente)
3. Ative as APIs: **Google Sheets API** e **Google Drive API**
4. Vá em **IAM & Admin → Service Accounts → Create Service Account**
5. Baixe o JSON da chave
6. **Compartilhe** as planilhas FM10 e FM11 com o email da Service Account
   (ex: `meu-servico@meu-projeto.iam.gserviceaccount.com`)
7. Se for usar upload no Drive, compartilhe a pasta de destino também
8. Cole o conteúdo do JSON na variável `GOOGLE_SERVICE_ACCOUNT_JSON`

## Deploy no Railway

1. Crie um repo Git e faça push (sem o .env!)
2. No Railway, crie um novo projeto a partir do repo
3. Nas **Variables** do servico, adicione:
   - `GOOGLE_SERVICE_ACCOUNT_JSON` -> conteudo do JSON da Service Account
   - `GDRIVE_SOURCE_FOLDER_ID` -> ID da pasta **pai** no Drive (contem subpastas mensais)
   - `GDRIVE_OUTPUT_FOLDER_ID` -> ID da pasta de destino no Drive (outputs)
   - `DATA_DIR` -> `/app/data` (ou outro caminho)
   - `TIMEZONE` -> `America/Sao_Paulo`
   - `SOURCE_MONTH` -> opcional, forca mes/ano (ex: `01-2026` ou `2026-01`)
4. O Procfile ja configura como worker (sem porta HTTP)

## Persistência de dados no Railway

O Railway não persiste o filesystem entre deploys. Para o `output.xlsx`
(que acumula dados entre execuções), você tem duas opções:

- **Google Drive como fonte de verdade**: o script faz upload do output.xlsx
  para o Drive. No início da execução, pode baixar o arquivo mais recente.
- **Volume do Railway**: crie um volume persistente e monte em `/app/data`.


## Estrutura de pastas no Drive (origem)

A pasta configurada em `GDRIVE_SOURCE_FOLDER_ID` Ã© o **folder pai**.
Dentro dela, o processo procura a subpasta do mÃªs no formato `MM-AAAA` (ex: `04-2026`).
Ã‰ nessa subpasta mensal que devem ficar os arquivos `.txt` de entrada.

Se vocÃª precisar rodar um mÃªs especÃ­fico, use `SOURCE_MONTH` (ex: `03-2026`).

## Rodar local

```bash
pip install -r requirements.txt
# Preencha o .env com suas credenciais
python main.py
```


