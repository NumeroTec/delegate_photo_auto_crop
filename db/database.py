import pymysql
from pymysql.cursors import DictCursor
from config import Config


def get_connection(database: str = None):
    """Create a new MySQL connection.
    If `database` is provided, it overrides the default DB_NAME from Config.
    """
    db_name = database if database else Config.DB_NAME
    return pymysql.connect(
        host=Config.DB_HOST,
        port=Config.DB_PORT,
        user=Config.DB_USER,
        password=Config.DB_PASSWORD,
        database=db_name,
        cursorclass=DictCursor,
        autocommit=False
    )
