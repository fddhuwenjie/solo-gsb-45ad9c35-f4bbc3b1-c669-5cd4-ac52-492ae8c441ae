import os


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:////data/swiss.db",
)

# SSE 心跳间隔（秒）：无消息时定期发送 ":" 注释保活
SSE_HEARTBEAT_SECONDS = float(os.getenv("SSE_HEARTBEAT_SECONDS", "15"))

# 事件分页接口每页最大条数
EVENTS_PAGE_LIMIT_CEILING = int(os.getenv("EVENTS_PAGE_LIMIT_CEILING", "200"))
