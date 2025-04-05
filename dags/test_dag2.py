from airflow import DAG
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.operators.python import PythonOperator
from datetime import datetime

source_table = "prog3"
staging_table = "stg_prog3"
transformation_table = "trans_prog3"

default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'start_date': datetime(2024, 3, 18),
    'retries': 0,
}


def get_postgres_hooks():
    """Helper function to return PostgresHook instances."""
    src_pg_hook = PostgresHook(postgres_conn_id='source_db')
    dest_pg_hook = PostgresHook(postgres_conn_id='destination_db')
    return src_pg_hook, dest_pg_hook


def create_staging_table(source_table, staging_table, **kwargs):
    src_pg_hook, dest_pg_hook = get_postgres_hooks()

    get_schema_query = f"""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = '{source_table}';
    """
    schema_result = src_pg_hook.get_records(get_schema_query)

    columns_definition = ", ".join([f'"{col[0]}" {col[1]}' for col in schema_result])
    create_table_query = f"""
        CREATE SCHEMA IF NOT EXISTS staging;
        CREATE TABLE IF NOT EXISTS staging.{staging_table} ({columns_definition});
    """
    
    dest_pg_hook.run(create_table_query)


def truncate_staging_table(staging_table, **kwargs):
    _, dest_pg_hook = get_postgres_hooks()
    
    truncate_query = f"TRUNCATE TABLE staging.{staging_table};"
    dest_pg_hook.run(truncate_query)


def transfer_data(source_table, staging_table, **kwargs):
    src_pg_hook, dest_pg_hook = get_postgres_hooks()

    # Extract data from source
    extract_query = f"SELECT * FROM {source_table};"
    source_data = src_pg_hook.get_records(extract_query)  # Fetch data

    if not source_data:
        raise ValueError(f"No data found in source table: {source_table}")

    # Load data into destination
    dest_pg_hook.insert_rows(f"staging.{staging_table}", source_data)


def move_to_transformation(staging_table, transformation_table, **kwargs):
    _, dest_pg_hook = get_postgres_hooks()


    create_table_query = f"""
    CREATE TABLE IF NOT EXISTS transformation.{transformation_table} (LIKE staging.{staging_table});
    ALTER TABLE transformation.{transformation_table} ADD CONSTRAINT uid_pk PRIMARY KEY (uid);
    """
    
    dest_pg_hook.run(create_table_query)

    get_columns_query = f"""
        SELECT column_name FROM information_schema.columns 
        WHERE table_schema = 'staging' 
        AND table_name = '{staging_table}'
        AND column_name <> 'uid';
    """
    columns = [row[0] for row in dest_pg_hook.get_records(get_columns_query)]

    # Create dynamic UPDATE clause
    update_clause = ", ".join([f'"{col}" = EXCLUDED."{col}"' for col in columns])

    # Upsert query
    upsert_query = f"""
        INSERT INTO transformation.{transformation_table}
        SELECT * FROM staging.{staging_table}
        ON CONFLICT (uid) DO UPDATE 
        SET {update_clause}
        WHERE transformation.{transformation_table}.lastupdated < EXCLUDED.lastupdated;
    """
    dest_pg_hook.run(upsert_query)

with DAG(
    'test_dag2',
    default_args=default_args,
    schedule_interval="0 0 * * *",
    catchup=False,
) as dag:

    create_staging = PythonOperator(
        task_id='create_staging_table',
        python_callable=create_staging_table,
        op_kwargs={'source_table': source_table, 'staging_table': staging_table},
    )

    truncate_staging = PythonOperator(
        task_id='truncate_staging_table',
        python_callable=truncate_staging_table,
        op_kwargs={'staging_table': staging_table},
    )

    transfer_data_task = PythonOperator(
        task_id='transfer_data',
        python_callable=transfer_data,
        op_kwargs={'source_table': source_table, 'staging_table': staging_table},
    )

    move_to_transformation_task = PythonOperator(
        task_id='move_to_transformation',
        python_callable=move_to_transformation,
        op_kwargs={'staging_table': staging_table, 'transformation_table': transformation_table},
    )

    create_staging >> truncate_staging >> transfer_data_task >> move_to_transformation_task
