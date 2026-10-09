import pymysql
from db.database import get_connection
from config import Config


def get_active_upcoming_conferences():
    """Fetch active and upcoming conferences ordered by start date.

    Criteria:
      - is_active = 1
      - conference start date >= today (using conf_start_dt if present else DATE(conf_start_time))
      - ordered by conference start date ASC
    Returns list of dicts with conf_id, conf_name, conf_key, conf_start_dt, conf_start_time, conf_end_dt
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            sql = """
                SELECT conf_id, conf_name, conf_key, conf_start_dt, conf_start_time, conf_end_dt, conf_end_time
                FROM conference
                WHERE is_active = 1
                  AND COALESCE(conf_start_dt, DATE(conf_start_time)) >= CURDATE()
                ORDER BY COALESCE(conf_start_dt, DATE(conf_start_time)) ASC, conf_start_time ASC
            """
            cur.execute(sql)
            rows = cur.fetchall()
            return rows
    finally:
        conn.close()


def get_conference_by_id(conf_id: str):
    """Retrieve single conference row by conf_id."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            sql = """
                SELECT conf_id, conf_name, conf_key, conf_start_dt, conf_start_time, conf_end_dt, conf_schema
                FROM conference
                WHERE conf_id = %s
                LIMIT 1
            """
            cur.execute(sql, (conf_id,))
            return cur.fetchone()
    finally:
        conn.close()


def get_registration_stats(conf_schema: str) -> dict:
    """Live registration/photo-verification counts for one conference schema.

    All counts scoped to registered delegates (del_status_id=2):
      registered : total registered delegates
      photos     : registered + del_img_filename present
      done       : registered + profile_photo_status=2 (verified)
      pending    : registered + profile_photo_status=0 + has photo (auto-crop queue)
      rejected   : registered + profile_photo_status=1
    Returns zeros on any DB error so the dashboard never 500s.
    """
    stats = {'registered': 0, 'photos': 0, 'done': 0, 'pending': 0, 'rejected': 0, 'no_photo': 0}
    if not conf_schema:
        return stats
    try:
        conn = get_connection(database=conf_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COUNT(*) AS registered,
                        SUM(CASE WHEN del_img_filename IS NOT NULL AND del_img_filename != '' THEN 1 ELSE 0 END) AS photos,
                        SUM(CASE WHEN profile_photo_status = 2 THEN 1 ELSE 0 END) AS done,
                        SUM(CASE WHEN profile_photo_status = 0 AND del_img_filename IS NOT NULL AND del_img_filename != '' THEN 1 ELSE 0 END) AS pending,
                        SUM(CASE WHEN profile_photo_status = 1 THEN 1 ELSE 0 END) AS rejected,
                        SUM(CASE WHEN del_img_filename IS NULL OR del_img_filename = '' THEN 1 ELSE 0 END) AS no_photo
                    FROM delegates
                    WHERE del_status_id = 2
                """)
                row = cur.fetchone() or {}
                for k in stats:
                    try:
                        stats[k] = int(row.get(k) or 0)
                    except (TypeError, ValueError):
                        stats[k] = 0
        finally:
            conn.close()
    except Exception:
        pass
    return stats


def get_conference_schema(conf_id: str, conf_key: str) -> str:
    """Retrieve the schema (database) name for a given conference.
    Assumes a table `conferences` exists in the main database with columns:
        - conf_id
        - conf_key
        - schema_name (or similar) that stores the delegate DB name.
    Adjust the column name as needed.
    """
    conn = get_connection()  # connect to main DB (default DB_NAME)
    try:
        with conn.cursor() as cur:
            sql = """
                SELECT schema_name
                FROM conferences
                WHERE conf_id = %s AND conf_key = %s
                LIMIT 1
            """
            cur.execute(sql, (conf_id, conf_key))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Conference not found for id={conf_id}, key={conf_key}")
            return row['schema_name']
    finally:
        conn.close()
