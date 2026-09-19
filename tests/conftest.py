"""共享测试工具。"""
from __future__ import annotations

import time
from typing import Any

from fastapi.testclient import TestClient

TERMINAL_STATUSES = {"succeeded", "failed"}


def wait_for_task(
    client: TestClient,
    task_id: str,
    *,
    expected: set[str] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """轮询直到任务进入期望状态（默认任一终态），返回任务 JSON。

    长任务改由专用有界线程池异步执行后，POST 会立即返回，不再像
    `BackgroundTasks` 那样等任务跑完才回响应 —— "提交后立刻就是 succeeded"
    这个断言不再成立，必须轮询。
    """
    wanted = expected or TERMINAL_STATUSES
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = client.get(f"/tasks/{task_id}", headers=headers)
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in wanted:
            return last
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 在 {timeout}s 内未到达 {wanted}，最后状态：{last}")
