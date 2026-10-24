@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Criando ambiente virtual...
  py -3.12 -m venv .venv 2>nul || py -3.11 -m venv .venv 2>nul || python -m venv .venv
  if errorlevel 1 goto erro
)

if not exist ".venv\.instalado" (
  echo Instalando dependencias. Na primeira vez isso pode levar alguns minutos...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto erro
  echo ok> ".venv\.instalado"
)

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo.
  echo Arquivo .env criado. Ajuste OLLAMA_URL e ADMIN_KEY nele se necessario.
  echo.
)

echo Iniciando o chatbot em http://localhost:8000  (Ctrl+C para encerrar)
echo Na primeira execucao o modelo de embeddings e baixado e os documentos sao indexados.
".venv\Scripts\python.exe" -m uvicorn app.main:criar_app --factory --host 0.0.0.0 --port 8000
goto fim

:erro
echo.
echo Falha na preparacao do ambiente. Veja as mensagens acima e a secao "Solucao de problemas" do README.
pause

:fim
endlocal
