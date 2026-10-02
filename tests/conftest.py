import os
import tempfile

import pytest

# 必须在导入 app 之前设置数据库路径
_tmp = tempfile.mkdtemp(prefix="swiss-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.database import Base, engine
    from app.main import app

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    with TestClient(app) as c:
        yield c
