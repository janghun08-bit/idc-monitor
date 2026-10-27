"""Database: MySQL 저장.

PyMySQL 연결 하나를 여러 ClientHandler 쓰레드가 같이 쓰면 쿼리가 섞이므로 Lock으로 한 번에 하나씩 실행한다.
연결이 끊겨 있으면 다음 쿼리 때 다시 연결한다 (DB가 잠깐 죽어도 서버는 계속 동작).

DB_USER가 비어 있으면 NullDatabase를 써서 DB 없이도 서버를 돌릴 수 있게 한다.
"""
import logging
import threading
import time
from datetime import datetime, timezone

from common import config

log = logging.getLogger("db")


class DatabaseError(Exception):
    """저장 실패. ClientHandler는 이 경우 ACK를 보내지 않는다."""


def _to_utc_datetime(ts: float) -> datetime:
    # DB에는 UTC로 저장하고, Grafana가 브라우저 시간대(한국)로 바꿔서 보여준다
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)


class Database:
    RETRY_INTERVAL = 10  # 연결 실패 후 이 시간(초) 동안은 접속을 시도하지 않고 바로 실패 처리

    def __init__(self, host: str, port: int, user: str, password: str, name: str):
        self.params = dict(host=host, port=port, user=user, password=password, database=name)
        self.conn = None
        self.retry_at = 0.0
        self.lock = threading.Lock()

    @classmethod
    def from_config(cls):
        if not config.get("DB_USER"):
            log.warning("DB_USER가 비어 있음 → DB 없이 실행 (저장 생략)")
            return NullDatabase()
        return cls(
            host=config.get("DB_HOST", "127.0.0.1"),
            port=config.get_int("DB_PORT", 3306),
            user=config.get("DB_USER"),
            password=config.get("DB_PASSWORD", ""),
            name=config.get("DB_NAME", "idc_monitor"),
        )

    def _connection(self):
        import pymysql  # NullDatabase만 쓸 때는 pymysql이 없어도 되도록 여기서 import
        if self.conn is None:
            # DB가 죽어 있을 때 매번 접속을 기다리면 경보 판정까지 늦어지므로, 잠시 동안은 바로 포기한다
            if time.monotonic() < self.retry_at:
                raise DatabaseError("DB 연결 끊김 (재연결 대기 중)")
            self.retry_at = time.monotonic() + self.RETRY_INTERVAL
            self.conn = pymysql.connect(
                **self.params, charset="utf8mb4", autocommit=True,
                connect_timeout=3, read_timeout=5, write_timeout=5,
            )
            log.info("🗄️ MySQL 연결됨: %s:%s/%s", self.params["host"], self.params["port"], self.params["database"])
        return self.conn

    def _execute(self, sql: str, args: tuple) -> int:
        import pymysql
        with self.lock:
            try:
                with self._connection().cursor() as cur:
                    return cur.execute(sql, args)
            except pymysql.Error as e:
                # 연결이 깨졌을 수 있으니 버리고, 다음 쿼리 때 새로 연결한다
                if self.conn is not None:
                    try:
                        self.conn.close()
                    except Exception:
                        pass
                self.conn = None
                raise DatabaseError(str(e)) from e

    def check(self):
        """서버 시작 시 연결 확인. 실패해도 서버는 계속 뜬다."""
        try:
            self._execute("SELECT 1", ())
        except DatabaseError as e:
            log.warning("MySQL 연결 실패: %s (나중에 다시 시도)", e)

    def save_reading(self, agent_id: str, seq: int, ts: float, data: dict) -> bool:
        """저장하면 True, 이미 같은 (agent_id, seq)가 있으면 False."""
        rows = self._execute(
            "INSERT IGNORE INTO sensor_readings"
            " (agent_id, seq, ts, temperature, humidity, leak, distance_cm)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (agent_id, seq, _to_utc_datetime(ts), data.get("temperature"), data.get("humidity"),
             data.get("leak"), data.get("distance_cm")),
        )
        return rows == 1

    def save_event(self, agent_id: str, ts: float, event_type: str, level, message: str):
        self._execute(
            "INSERT INTO events (agent_id, ts, event_type, level, message) VALUES (%s, %s, %s, %s, %s)",
            (agent_id, _to_utc_datetime(ts), event_type, level, message[:255]),
        )


class NullDatabase:
    """DB 없이 실행할 때 쓰는 대역. 항상 '저장 성공'으로 처리한다."""

    def check(self):
        pass

    def save_reading(self, agent_id, seq, ts, data) -> bool:
        return True

    def save_event(self, agent_id, ts, event_type, level, message):
        pass
