"""异步速率限制器 - 基于 Semaphore + 令牌桶算法，支持 412 自适应退避"""

import asyncio
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class AsyncRateLimiter:
    """异步速率限制器

    结合 asyncio.Semaphore（并发控制）与令牌桶算法（RPS 控制），
    并在收到 412 响应时自动降低并发、冷却后逐步恢复。

    用法::

        limiter = AsyncRateLimiter(max_concurrent=5, max_rps=3)

        async with limiter:
            resp = await fetch(url)
            if resp.status == 412:
                limiter.report_412()
            else:
                limiter.report_success()
    """

    def __init__(
        self,
        max_concurrent: int = 5,
        max_rps: int = 3,
        cooldown: float = 30.0,
    ) -> None:
        """初始化速率限制器

        Args:
            max_concurrent: 最大并发请求数
            max_rps: 每秒最大请求数（令牌桶填充速率）
            cooldown: 412 退避基础冷却时间（秒），实际冷却时间会随连续 412 次数递增
        """
        self._original_max = max_concurrent
        self._current_max = max_concurrent
        self._max_rps = max_rps
        self._cooldown = cooldown

        # 并发信号量
        self._semaphore = asyncio.Semaphore(max_concurrent)

        # 令牌桶
        self._tokens: float = float(max_rps)
        self._last_refill: float = time.monotonic()
        self._bucket_lock = asyncio.Lock()

        # 412 自适应退避
        self._paused = asyncio.Event()
        self._paused.set()  # 初始不阻塞
        self._recovery_task: Optional[asyncio.Task] = None
        self._consecutive_412: int = 0  # 连续 412 计数，用于递增冷却
        self._412_lock = asyncio.Lock()  # 保护 _consecutive_412 和恢复任务
        self._last_412_time: float = 0  # 上次 412 时间戳，用于去重
        # 连续成功计数：每累计 _success_decay_threshold 次成功，
        # 将 _consecutive_412 减 1（逐步恢复，避免一次 412 后整个会话都处于高冷却）
        self._consecutive_success: int = 0
        self._success_decay_threshold: int = 20  # 每 20 次成功衰减 1 次 412 计数

    # ------------------------------------------------------------------
    # 令牌桶
    # ------------------------------------------------------------------

    async def _acquire_token(self) -> None:
        """等待直到令牌桶中有可用令牌"""
        while True:
            async with self._bucket_lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(
                    self._max_rps,
                    self._tokens + elapsed * self._max_rps,
                )
                self._last_refill = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return

            # 令牌不足，短暂等待后重试
            await asyncio.sleep(1.0 / self._max_rps)

    # ------------------------------------------------------------------
    # 412 自适应退避
    # ------------------------------------------------------------------

    async def report_412(self) -> None:
        """报告收到 412 响应，触发自适应退避

        - 5 秒内多次 412 只计一次（去重窗口，避免并发在途请求重复触发）
        - 连续 412 计数基于时间衰减：超过 300s 无 412 则自动重置为 0
        - 冷却时间随连续次数递增（30s → 60s → 120s → 240s → 300s 上限）
        - 将当前最大并发减半（最低 1）
        - 暂停所有新请求获取，进入冷却期
        """
        async with self._412_lock:
            now = time.monotonic()
            elapsed_since_last = now - self._last_412_time

            # 5 秒去重窗口：短时间内多次 412 只计一次
            if elapsed_since_last < 5.0:
                return

            # 时间衰减：超过 300s 无 412，重置计数（自然恢复）
            if elapsed_since_last > 300.0:
                self._consecutive_412 = 0

            self._last_412_time = now
            self._consecutive_412 += 1
            # 成功链断裂，重置连续成功计数
            self._consecutive_success = 0

            # 如果已在恢复中，先取消旧任务
            if self._recovery_task is not None and not self._recovery_task.done():
                self._recovery_task.cancel()

            # 降低并发
            new_max = max(1, self._current_max // 2)

            # 递增冷却：第 N 次连续 412 → cooldown * 2^(N-1)，上限 300s
            effective_cooldown = min(
                self._cooldown * (2 ** (self._consecutive_412 - 1)), 300.0
            )

            logger.warning(
                "412 自适应退避: 并发 %d → %d, 冷却 %.0fs (连续第 %d 次)",
                self._current_max,
                new_max,
                effective_cooldown,
                self._consecutive_412,
            )
            self._current_max = new_max

            # 暂停所有新获取
            self._paused.clear()

            # 启动冷却 + 恢复协程
            self._recovery_task = asyncio.ensure_future(
                self._cooldown_and_recover(effective_cooldown)
            )

    async def wait_recovery(self) -> None:
        """等待 412 冷却结束（供调用方在 412 重试前使用）"""
        await self._paused.wait()

    async def _cooldown_and_recover(self, cooldown: float) -> None:
        """冷却等待后逐步恢复并发"""
        try:
            await asyncio.sleep(cooldown)

            while self._current_max < self._original_max:
                self._current_max = min(
                    self._current_max + 1, self._original_max
                )
                logger.info(
                    "412 恢复: 并发提升至 %d/%d",
                    self._current_max,
                    self._original_max,
                )
                self._semaphore = asyncio.Semaphore(self._current_max)
                if self._current_max < self._original_max:
                    await asyncio.sleep(10.0)

            logger.info("412 恢复完成: 并发已回到 %d", self._original_max)
        except asyncio.CancelledError:
            pass
        finally:
            self._paused.set()

    # ------------------------------------------------------------------
    # 成功报告（逐步衰减 412 计数，避免一次 412 后整个会话都处于高冷却）
    # ------------------------------------------------------------------

    def report_success(self) -> None:
        """报告一次成功请求

        每累计 _success_decay_threshold 次连续成功，将 _consecutive_412 减 1
        （最低 0）。这样长时间无 412 时冷却时间能逐步回落，而不是只有 300s
        完全无 412 才重置。一旦再次 412，_consecutive_success 归零。
        """
        # _consecutive_412 为 0 时无需衰减
        if self._consecutive_412 <= 0:
            self._consecutive_success = 0
            return
        self._consecutive_success += 1
        if self._consecutive_success >= self._success_decay_threshold:
            self._consecutive_success = 0
            self._consecutive_412 = max(0, self._consecutive_412 - 1)
            logger.info(
                "412 计数衰减: %d → %d (连续 %d 次成功)",
                self._consecutive_412 + 1,
                self._consecutive_412,
                self._success_decay_threshold,
            )

    # ------------------------------------------------------------------
    # 异步上下文管理器
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "AsyncRateLimiter":
        """获取请求槽位：等待暂停解除 → 获取令牌 → 获取信号量"""
        # 1. 等待 412 冷却结束
        await self._paused.wait()

        # 2. 令牌桶限速
        await self._acquire_token()

        # 3. 并发信号量
        await self._semaphore.acquire()

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """释放请求槽位"""
        self._semaphore.release()
