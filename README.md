# Discador Api4Com — Deploy Railway

## Deploy em 5 minutos

### 1. Sobe no GitHub
- Cria repositório novo em github.com (pode ser privado)
- Faz upload de todos os arquivos desta pasta

### 2. Deploy no Railway
- Acessa railway.app e loga com GitHub
- New Project → Deploy from GitHub → seleciona o repositório
- Railway detecta o Procfile automaticamente

### 3. Variáveis de ambiente
No Railway: Settings → Variables → Add Variable:

| Variável | Valor |
|---|---|
| API4COM_TOKEN | seu token |
| RAMAL | 1005 |
| MAX_PARALLEL | 3 |

### 4. Pronto
Railway gera uma URL tipo: https://discador-production.up.railway.app

## Como usar
1. Acessa a URL
2. Arrasta sua planilha CSV ou Excel
3. Clica em Iniciar
4. Quando alguém atender, aparece o nome em destaque verde
5. Clica em Retomar para continuar discando
