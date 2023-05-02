from statsapi import *
import datetime as dt
from datetime import datetime
import json
import pathlib
import airflow
import requests
import requests.exceptions as requests_exceptions
import csv
from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator
from airflow.providers.postgres.operators.postgres import PostgresOperator
from airflow.providers.amazon.aws.transfers.sql_to_s3 import SqlToS3Operator
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
from tempfile import NamedTemporaryFile
from airflow.utils.db import provide_session
from airflow.models import XCom

dag = DAG(
    dag_id = "get_todays_gamesv2",
    start_date = dt.datetime(2023,4,1),
    end_date = dt.datetime(2023,10,1),
    schedule_interval=None,
    catchup=False
)


def _call_games(ti,
                 start_date=dt.datetime.now().strftime('%m/%d/%Y'),
                 end_date=dt.datetime.now().strftime('%m/%d/%Y'),
                 team=147):
    games = schedule(start_date=start_date,end_date=end_date,team=team)
    games_dict = {}
    i = 0
    for g in games:
        games_dict[i] = g
        i+=1
    ti.xcom_push(key=f'games',value=games_dict)

#@task.branch
def _check_game_today(ti):
    today_date = dt.datetime.now().strftime('%Y-%m-%d')
    print(today_date)
    games = ti.xcom_pull(key=f'games')
    game_date = next(iter(games.values()))['game_date']
    print(game_date)


def _write_insert_query(ti,ds):
    games = ti.xcom_pull(key=f'games')
    sql_query = ''
    for k,v in games.items():
        sql_query = sql_query + '\n' + f'''INSERT INTO games (away_name,home_name,away_probable_pitcher,home_probable_pitcher,venue_name,game_date)
                                   VALUES ('{v['away_name']}','{v['home_name']}','{v['away_probable_pitcher']}',
                                   '{v['home_probable_pitcher']}','{v['venue_name']}','{v['game_date']}');'''
    ti.xcom_push(key=f'insert_statements',value=sql_query)


def postgres_to_s3(ds):
    #https://www.youtube.com/watch?v=rcG4WNwi900
    #first query data from psql and save in text file
    hook = PostgresHook(postgres_conn_id="postgres_localhost")
    conn = hook.get_conn()
    cursor = conn.cursor()
    cursor.execute('select * from games;')
    with NamedTemporaryFile(mode='w') as f: #puts file in temp folder

    #with open('dags/test_data.txt','w') as f:
        csv_writer = csv.writer(f)
        csv_writer.writerow([i[0] for i in cursor.description])
        csv_writer.writerows(cursor)
        f.flush()
        cursor.close()
        conn.close()
        logging.info(f'Ran this on {ds}. Saved postgres data in text file: games.txt')
    #step 2: upload text file into s3
        s3_hook = S3Hook(aws_conn_id='aws_hook')
        s3_hook.load_file(
            filename=f.name,
            key=f'games/games_{ds}.csv',
            bucket_name='mlb-project',
            replace=True
        )
        logging.info(f'Test data file {f.name} has been pushed to S3')

print_start = BashOperator(
    task_id="print_start",
    bash_command="echo starting dag",
    dag=dag,
    do_xcom_push=False
)

call_games = PythonOperator(
    task_id="call_games",
    python_callable=_call_games,
    dag=dag
)

sql_to_s3 = PythonOperator(
    dag=dag,
    task_id="postgres_to_s3_task",
    python_callable=postgres_to_s3
)

print_end = BashOperator(
    task_id="print_end",
    bash_command="echo end dag!",
    dag=dag,
    do_xcom_push=False
)
@provide_session
def _delete_xcoms(session=None):
    num_rows_deleted = 0

    try:
        num_rows_deleted = session.query(XCom).delete()
        session.commit()
    except:
        session.rollback()

    print(f"Deleted {num_rows_deleted} XCom rows")

delete_xcoms = PythonOperator(task_id="delete_xcoms", python_callable=_delete_xcoms)

create_sql_table = PostgresOperator(
    task_id='create_sql_table_task',
    postgres_conn_id='postgres_localhost',
    sql = '''
        drop table if exists games;
        create table games (
            away_name VARCHAR(40),
            home_name VARCHAR(40),
            away_probable_pitcher VARCHAR(40),
            home_probable_pitcher VARCHAR(40),
            venue_name VARCHAR(40),
            game_date DATE
            );
    '''
)

write_insert_query= PythonOperator(
    task_id = 'write_insert_query_task',
    python_callable= _write_insert_query,
    dag=dag
)

exec_insert_query = PostgresOperator(
    task_id='exec_insert_query_task',
    postgres_conn_id='postgres_localhost',
    sql ='{{ ti.xcom_pull(key="insert_statements") }}'
)

check_game_today = PythonOperator(
    task_id='check_game_today_task',
    python_callable=_check_game_today,
    dag=dag
)

print_start >> [call_games, create_sql_table] >> check_game_today
call_games >> write_insert_query 
[create_sql_table,write_insert_query] >> exec_insert_query >> [sql_to_s3, delete_xcoms] >>  print_end
