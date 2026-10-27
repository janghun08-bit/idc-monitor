-- 컨테이너가 처음 만들어질 때 한 번만 실행된다 (데이터 볼륨이 비어 있을 때)
CREATE TABLE IF NOT EXISTS sensor_readings (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  agent_id VARCHAR(32) NOT NULL,
  seq INT NOT NULL,
  ts DATETIME NOT NULL,             -- 측정 시각 (UTC)
  temperature FLOAT,
  humidity FLOAT,
  leak BOOLEAN,
  distance_cm FLOAT,
  UNIQUE KEY uq_agent_seq (agent_id, seq),   -- 같은 데이터를 두 번 저장하지 않게
  KEY idx_ts (ts)                            -- Grafana가 시간 범위로 조회하므로
) DEFAULT CHARSET = utf8mb4;

CREATE TABLE IF NOT EXISTS events (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  agent_id VARCHAR(32) NOT NULL,
  ts DATETIME NOT NULL,             -- 발생 시각 (UTC)
  event_type VARCHAR(32) NOT NULL,  -- STATE_CHANGE, ACCESS, ALARM_CLEAR, AGENT_OFFLINE, AGENT_ONLINE
  level VARCHAR(16),                -- NORMAL, WARN, DANGER, ACCESS
  message VARCHAR(255),
  KEY idx_ts (ts)
) DEFAULT CHARSET = utf8mb4;
