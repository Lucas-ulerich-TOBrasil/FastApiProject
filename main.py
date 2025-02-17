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
        return SCHEMA_CACHE

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
    for row in rows:
        table, column, data_type = row
        schema_info += f'Tabela: ortocenter."{table}" - "{column}" ({data_type})\n'

    SCHEMA_CACHE = schema_info
    conn.close()
    return schema_info


# Função para validar a query gerada
def validar_sql_query(sql_query):
    if not sql_query.strip():
        raise HTTPException(status_code=400, detail="A query gerada está vazia.")

    if "SELECT" not in sql_query.upper():
        raise HTTPException(status_code=400, detail="A query gerada não é uma consulta válida.")

    if "FROM ortocenter" not in sql_query:
        raise HTTPException(status_code=400, detail="A query não faz referência ao schema correto.")

    return True


# Função para corrigir erros comuns na query gerada

def corrigir_query(sql_query):
    sql_query = sql_query.strip()

    # Remover escapes de barras invertidas antes de aspas
    sql_query = sql_query.replace('\\"', '"')

    # Remover marcações extras do ChatGPT
    sql_query = sql_query.replace("```sql", "").replace("```", "")

    # Remover referências duplicadas ao schema ortocenter
    sql_query = re.sub(r'\bortocenter\.ortocenter\.', 'ortocenter.', sql_query, flags=re.IGNORECASE)
    sql_query = re.sub(r'FROM\s+"?ortocenter"?"?\."?ortocenter"?"?\.', 'FROM ortocenter.', sql_query, flags=re.IGNORECASE)

    return sql_query

def generate_friendly_response(results, pergunta, query_sql):
    if isinstance(results, list) and len(results) == 1 and len(results[0]) == 1:
        valor = str(results[0][0])

        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Você é um assistente amigável e profissional. Responda de forma clara e simpática, sem exageros, como um atendimento atencioso e direto."},
                {"role": "user", "content": f"A resposta para '{pergunta}' é {valor}. Gere uma resposta natural, amigável e objetiva, sem exageros."}
            ]
        )
        return response.choices[0].message.content.strip(), query_sql

    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "Você é um assistente amigável e profissional. Responda de forma clara e simpática, sem exageros, como um atendimento atencioso e direto."},
            {"role": "user", "content": f"Os resultados SQL foram: {results}. Crie uma resposta natural e amigável, sem exageros, baseada nesses dados."}
        ]
    )
    return response.choices[0].message.content.strip(), query_sql


# Gerar SQL com IA baseada no esquema do banco
def generate_sql_query(pergunta, schema):
    # Obtém o ano atual
    ano_atual = datetime.today().year

    # Dicionário de meses para garantir que o ano atual seja usado
    meses = {
        "janeiro": "01", "fevereiro": "02", "março": "03", "abril": "04", "maio": "05", "junho": "06",
        "julho": "07", "agosto": "08", "setembro": "09", "outubro": "10", "novembro": "11", "dezembro": "12"
    }

    # Verifica se a pergunta menciona um mês específico
    for mes_nome, mes_num in meses.items():
        if f"mês de {mes_nome}" in pergunta.lower():
            return f""" 
            SELECT COUNT(*) 
            FROM ortocenter."AGENDA" 
            WHERE "dataagendamento" >= TIMESTAMP '{ano_atual}-{mes_num}-01 00:00:00' 
            AND "dataagendamento" <= TIMESTAMP '{ano_atual}-{mes_num}-28 23:59:59';
            """

    # Se a pergunta for sobre "último mês", calcular corretamente
    if "último mês" in pergunta.lower():
        return f""" 
        SELECT COUNT(*) 
        FROM ortocenter."AGENDA" 
        WHERE "dataagendamento" >= date_trunc('month', current_date - interval '1 month') 
        AND "dataagendamento" <= date_trunc('month', current_date) - interval '1 second';
        """

    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system",
             "content": f"""Você é um assistente SQL para PostgreSQL. Gere apenas consultas SQL válidas.
             Sempre use o schema 'ortocenter' antes dos nomes das tabelas e envolva os nomes das tabelas em ASPAS DUPLAS.
             Se a pergunta envolver contagem, use COUNT(*).
             Para consultas de datas, utilize a coluna "dataagendamento" e SEMPRE inclua o ano {ano_atual} e o dia final até '23:59:59.999'.
             Sempre escreva queries SQL válidas, evite erros de sintaxe e mantenha a compatibilidade com PostgreSQL."""},
            {"role": "user",
             "content": f"Aqui está o esquema do banco:\n{schema}\n\nTransforme a seguinte solicitação em SQL: {pergunta}"}
        ]
    )

    sql_query = response.choices[0].message.content.strip()
    sql_query = corrigir_query(sql_query)  # Aplica as correções automáticas

    return sql_query

# Executar a query gerada
def execute_sql_query(query):
    conn = conectar_bd()
    cursor = conn.cursor()
    try:
        cursor.execute(query)
        rows = cursor.fetchall()
        conn.close()
        return rows
    except Exception as e:
        logging.error(f"Erro ao executar a consulta SQL: {str(e)}")
        return {"erro": str(e), "query_sql": query}


# Endpoint principal para perguntas
@app.get("/query")
def executar_consulta(pergunta: str = Query(..., description="Pergunta em linguagem natural")):
    schema = get_db_schema()
    sql_query = generate_sql_query(pergunta, schema)

    # Valida a query antes de executar
    validar_sql_query(sql_query)

    results = execute_sql_query(sql_query)

    if "erro" in results:
        return {
            "erro": results["erro"],
            "query_sql": results["query_sql"]
        }

    resposta, query_sql = generate_friendly_response(results, pergunta, sql_query)

    return {
        "resposta": resposta,
        "query_sql": query_sql
    }

# Endpoint de teste
@app.get("/")
def home():
    return {"message": "API funcionando"}
