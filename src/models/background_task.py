"""
models/background_task.py

Persistente spiegel van de in-memory achtergrondtaken-registry
(services/builtin/tools/background_tasks.py). Elke statusovergang wordt
best-effort naar deze tabel geschreven zodat de takenlijst een herstart
overleeft; bij boot worden rijen teruggeladen en worden taken die nog
"running" stonden als onderbroken gemarkeerd.
"""
from sqlalchemy import BigInteger, Boolean, Column, String
from sqlalchemy.types import JSON

from db.database import Base


class BackgroundTask(Base):
    __tablename__ = "background_tasks"

    id = Column(String(32), primary_key=True)  # "bg-<hex12>"
    status = Column(String(16), nullable=False, default="running", index=True)  # running/done/error/cancelled
    owner_thread = Column(String(128), nullable=True, index=True)
    assistant = Column(String(255), nullable=True)
    task_preview = Column(String(512), nullable=True)
    created_at = Column(BigInteger, nullable=True)  # epoch ms, zoals de registry
    finished_at = Column(BigInteger, nullable=True)
    result = Column(JSON, nullable=True)
    acknowledged = Column(Boolean, nullable=False, default=False)
