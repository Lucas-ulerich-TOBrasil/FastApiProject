import openai
import psycopg2
import re
import os
from fastapi import FastAPI, Query
from dotenv import load_dotenv

# Carregar variáveis do .env
load_dotenv()

# Configuração do Banco de Dados PostgreSQL
DB_HOST = os.getenv("DB_HOST")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

app = FastAPI(title="NL2SQL API")

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
        cursor.execute("SET search_path TO ortocenter;")  # Define o schema correto
        return conn
    except Exception as e:
        print(f"Erro ao conectar ao banco: {e}")  # Log do erro no console
        raise Exception("Erro ao conectar ao banco de dados")  # Garante que o erro será tratado

# Obter esquema do banco PostgreSQL
def get_db_schema():
    conn = conectar_bd()
    if conn is None:
        return "Erro ao conectar ao banco."

    cursor = conn.cursor()
    schema_info = ""

    cursor.execute("""
        SELECT table_name, column_name, data_type 
        FROM information_schema.columns 
        WHERE table_schema = 'ortocenter'
        ORDER BY table_name
    """)

    rows = cursor.fetchall()

    current_table = None
    for row in rows:
        table = row[0]
        if table != current_table:
            schema_info += f"\nTabela: ortocenter.{table}\n"
            current_table = table
        schema_info += f" - {row[1]} ({row[2]})\n"

    conn.close()
    return schema_info.strip()

# Gerar SQL com IA baseada no esquema do banco
def generate_sql_query(pergunta, schema):
    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system",
             "content": "Você é um assistente SQL para PostgreSQL. Gere apenas consultas SQL válidas. Sempre use o schema 'ortocenter' antes dos nomes das tabelas."},
            {"role": "user",
             "content": f"Aqui está o esquema do banco:\n{schema}\n\nTransforme a seguinte solicitação em SQL: {pergunta}"}
        ]
    )

    sql_query = response.choices[0].message.content.strip()
    sql_query = re.sub(r"```sql|```", "", sql_query).strip()

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
        return f"Erro ao executar a consulta: {str(e)}"

# Transformar o resultado SQL em linguagem natural
def interpret_results(results):
    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "Você interpreta resultados SQL em frases claras para usuários."},
            {"role": "user", "content": f"Os resultados SQL foram: {results}. Explique em linguagem natural."}
        ]
    )
    return response.choices[0].message.content.strip()

# Endpoint principal para perguntas
@app.get("/query")
def executar_consulta(pergunta: str = Query(..., description="Pergunta em linguagem natural")):
    schema = get_db_schema()
    sql_query = generate_sql_query(pergunta, schema)
    results = execute_sql_query(sql_query)
    resposta = interpret_results(results)
    return {"resposta": resposta}

# Endpoint de teste
@app.get("/")
def home():
    return {"message": "API funcionando"}
