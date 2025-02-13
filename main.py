import openai
import psycopg2
import re
import os
import logging
from fastapi import FastAPI, Query, HTTPException
from dotenv import load_dotenv
from datetime import datetime, timedelta
from langdetect import detect

# Carregar variáveis do .env
load_dotenv()

# Configuração do Banco de Dados PostgreSQL
DB_HOST = os.getenv("DB_HOST")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Configuração de logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

app = FastAPI(title="NL2SQL API")

# Cache para esquema do banco
SCHEMA_CACHE = None

# Conectar ao PostgreSQL
def conectar_bd():
    try:
        conn = psycopg2.connect(
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            host=DB_HOST
        )
        cursor = conn.cursor()
        cursor.execute("SET search_path TO ortocenter;")
        return conn
    except Exception as e:
        logging.error(f"Erro ao conectar ao banco: {e}")
        raise HTTPException(status_code=500, detail="Erro ao conectar ao banco de dados")

# Obter esquema do banco PostgreSQL (com cache)
def get_db_schema():
    global SCHEMA_CACHE
    if SCHEMA_CACHE:
        return SCHEMA_CACHE  # Retorna o esquema armazenado para evitar consultas repetidas

    conn = conectar_bd()
    if conn is None:
        return "Erro ao conectar ao banco."

    cursor = conn.cursor()
    cursor.execute("""
        SELECT table_name, column_name, data_type 
        FROM information_schema.columns 
        WHERE table_schema = 'ortocenter'
        ORDER BY table_name
    """)

    rows = cursor.fetchall()
    schema_info = ""
    current_table = None
    table_columns = {}

    for row in rows:
        table = row[0]
        column = row[1]
        if table != current_table:
            schema_info += f'\nTabela: ortocenter."{table}"\n'
            current_table = table
        schema_info += f' - "{column}" ({row[2]})\n'
        table_columns.setdefault(table, []).append(column)

    SCHEMA_CACHE = schema_info  # Atualiza o cache
    conn.close()
    return schema_info

# Validar se a tabela e as colunas geradas pela IA existem no banco
def validar_sql_query(sql_query):
    schema = get_db_schema()
    tabelas = re.findall(r'ortocenter\."(\w+)"', sql_query)

    for tabela in tabelas:
        if f'ortocenter."{tabela}"' not in schema:
            raise HTTPException(status_code=400, detail=f"Tabela '{tabela}' não encontrada no banco de dados.")

# Identificar datas no input
def extrair_datas(pergunta):
    hoje = datetime.today()
    inicio_semana = hoje - timedelta(days=hoje.weekday())  # Segunda-feira
    fim_semana = inicio_semana + timedelta(days=6)  # Domingo

    if "essa semana" in pergunta.lower():
        return inicio_semana.strftime("%Y-%m-%d"), fim_semana.strftime("%Y-%m-%d")

    match = re.findall(r"(\d{1,2})\s*de\s*(janeiro|fevereiro|março|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)", pergunta, re.IGNORECASE)
    if match:
        meses = {
            "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4, "maio": 5, "junho": 6,
            "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12
        }
        datas = []
        for dia, mes_nome in match:
            mes = meses.get(mes_nome.lower())
            datas.append(f"{hoje.year}-{mes:02d}-{int(dia):02d}")

        if len(datas) == 2:
            return datas[0], datas[1]
        elif len(datas) == 1:
            return datas[0], datas[0]

    return None, None


# Gerar SQL com IA baseada no esquema do banco (correção de datas)
def generate_sql_query(pergunta, schema):
    data_inicio, data_fim = extrair_datas(pergunta)

    if data_inicio and data_fim:
        # Garante que o último dia seja incluído completamente até 23:59:59
        data_fim = f"{data_fim} 23:59:59"
        pergunta += f" (Considerando o intervalo de datas: {data_inicio} até {data_fim})"

    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system",
             "content": """Você é um assistente SQL para PostgreSQL. Gere apenas consultas SQL válidas.
             Sempre use o schema 'ortocenter' antes dos nomes das tabelas e envolva os nomes das tabelas em ASPAS DUPLAS.
             Se a pergunta envolver contagem, use COUNT(*).
             Para consultas de datas, utilize a coluna "dataagendamento" e SEMPRE inclua o dia final até '23:59:59'."""},
            {"role": "user",
             "content": f"Aqui está o esquema do banco:\n{schema}\n\nTransforme a seguinte solicitação em SQL: {pergunta}"}
        ]
    )

    sql_query = response.choices[0].message.content.strip()
    sql_query = re.sub(r"```sql|```", "", sql_query).strip()
    sql_query = re.sub(r'(\bortocenter\.\b)(\w+)', r'\1"\2"', sql_query)

    validar_sql_query(sql_query)  # Validação da query antes de executar

    return sql_query


# Executar a query gerada
def execute_sql_query(query):
    conn = conectar_bd()
    if conn is None:
        return "Erro ao conectar ao banco."

    cursor = conn.cursor()
    try:
        cursor.execute(query)
        rows = cursor.fetchall()
        conn.close()
        return rows
    except Exception as e:
        logging.error(f"Erro ao executar a consulta SQL: {str(e)}")
        return "Erro ao executar a consulta."

# Transformar o resultado SQL em resposta curta
def interpret_results(results):
    if isinstance(results, list) and len(results) == 1 and len(results[0]) == 1:
        return str(results[0][0])  # Retorna apenas o número diretamente

    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "Se for um número, retorne apenas o número. Caso contrário, explique resumidamente."},
            {"role": "user", "content": f"Os resultados SQL foram: {results}. Resuma a resposta da melhor forma possível."}
        ]
    )
    return response.choices[0].message.content.strip()

# Detectar idioma da pergunta
def detectar_idioma(pergunta):
    try:
        return detect(pergunta)
    except:
        return "pt"  # Padrão para português

# Endpoint principal para perguntas
@app.get("/query")
def executar_consulta(pergunta: str = Query(..., description="Pergunta em linguagem natural")):
    idioma = detectar_idioma(pergunta)
    if idioma != "pt":
        raise HTTPException(status_code=400, detail="Apenas perguntas em português são suportadas no momento.")

    schema = get_db_schema()
    sql_query = generate_sql_query(pergunta, schema)
    results = execute_sql_query(sql_query)
    resposta = interpret_results(results)
    return {"resposta": resposta}

# Endpoint de teste
@app.get("/")
def home():
    return {"message": "API funcionando"}
