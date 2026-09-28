"""计划执行模型模块。

定义了任务（Task）和执行计划（ExecutionPlan）的数据模型，
支持任务依赖管理、拓扑排序、并行批次计算等功能。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum


# ============================================================
# 枚举类型定义
# ============================================================


class TaskType(StrEnum):
    """任务类型枚举，定义任务可执行的操作种类。"""

    PLANNING = "PLANNING"           # 规划类任务
    FILE_READ = "FILE_READ"         # 文件读取
    FILE_WRITE = "FILE_WRITE"       # 文件写入
    COMMAND = "COMMAND"             # 执行外部命令
    ANALYSIS = "ANALYSIS"           # 分析类任务（默认类型）
    VERIFICATION = "VERIFICATION"   # 验证/检查类任务


class TaskStatus(StrEnum):
    """任务状态枚举，表示单个任务在其生命周期中的状态。"""

    PENDING = "PENDING"         # 等待执行
    RUNNING = "RUNNING"         # 正在执行
    COMPLETED = "COMPLETED"     # 已成功完成
    FAILED = "FAILED"           # 执行失败
    SKIPPED = "SKIPPED"         # 已跳过


class PlanStatus(StrEnum):
    """执行计划整体状态枚举。"""

    CREATED = "CREATED"         # 已创建，尚未开始
    RUNNING = "RUNNING"         # 正在执行中
    COMPLETED = "COMPLETED"     # 全部完成
    FAILED = "FAILED"           # 执行失败
    CANCELLED = "CANCELLED"     # 已取消


# ============================================================
# 数据模型
# ============================================================


@dataclass(slots=True)
class Task:
    """单个可执行任务。

    Attributes:
        id: 任务唯一标识。
        description: 任务描述。
        type: 任务类型，默认为 ANALYSIS。
        dependencies: 当前任务所依赖的其他任务 ID 列表（前置任务）。
        dependents: 依赖当前任务的其他任务 ID 列表（后续任务）。
        status: 任务当前状态。
        result: 任务成功完成后的输出结果。
        error: 任务失败时的错误信息。
        start_time: 任务开始时间戳（Unix 秒）。
        end_time: 任务结束时间戳（Unix 秒）。
    """

    id: str
    description: str
    type: TaskType = TaskType.ANALYSIS
    dependencies: list[str] = field(default_factory=list)
    dependents: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""
    error: str = ""
    start_time: float = 0.0
    end_time: float = 0.0

    def add_dependency(self, task_id: str) -> None:
        """添加一个前置依赖任务（去重）。"""
        if task_id not in self.dependencies:
            self.dependencies.append(task_id)

    def add_dependent(self, task_id: str) -> None:
        """添加一个后续依赖任务（去重）。"""
        if task_id not in self.dependents:
            self.dependents.append(task_id)

    def mark_started(self) -> None:
        """将任务标记为正在执行，并记录开始时间。"""
        self.status = TaskStatus.RUNNING
        self.start_time = time.time()

    def mark_completed(self, result: str) -> None:
        """将任务标记为已完成，记录结果和结束时间。"""
        self.status = TaskStatus.COMPLETED
        self.result = result
        self.end_time = time.time()

    def mark_failed(self, error: str) -> None:
        """将任务标记为失败，记录错误信息和结束时间。"""
        self.status = TaskStatus.FAILED
        self.error = error
        self.end_time = time.time()

    def mark_skipped(self) -> None:
        """将任务标记为已跳过，并记录结束时间。"""
        self.status = TaskStatus.SKIPPED
        self.end_time = time.time()

    def is_executable(self, all_tasks: dict[str, Task]) -> bool:
        """判断当前任务是否可以立即执行。

        条件：任务状态为 PENDING，且所有前置依赖任务均已完成。

        Args:
            all_tasks: 计划中所有任务的字典，用于查询依赖状态。

        Returns:
            True 表示任务可执行，否则返回 False。
        """
        if self.status != TaskStatus.PENDING:
            return False
        return all(
            dep_id in all_tasks and all_tasks[dep_id].status == TaskStatus.COMPLETED
            for dep_id in self.dependencies
        )


@dataclass(slots=True)
class ExecutionPlan:
    """执行计划，管理一组具有依赖关系的任务。

    支持 DAG（有向无环图）形式的任务调度，包括拓扑排序、
    并行批次划分、进度追踪等功能。

    Attributes:
        id: 计划唯一标识。
        goal: 计划目标描述。
        tasks: 计划中包含的所有任务，以任务 ID 为键。
        status: 计划整体状态。
        summary: 计划摘要。
        start_time: 计划开始时间戳。
        end_time: 计划结束时间戳。
        _execution_order: 缓存的拓扑排序结果（惰性计算）。
    """

    id: str
    goal: str
    tasks: dict[str, Task] = field(default_factory=dict)
    status: PlanStatus = PlanStatus.CREATED
    summary: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    _execution_order: list[str] = field(default_factory=list)

    def add_task(self, task: Task) -> None:
        """向计划中添加一个任务，并自动维护双向依赖关系。

        添加任务后会清除缓存的执行顺序，需要时重新计算。

        Args:
            task: 要添加的任务对象。
        """
        self.tasks[task.id] = task
        # 将当前任务注册为其前置任务的后继（dependent）
        for dep_id in task.dependencies:
            dep = self.tasks.get(dep_id)
            if dep:
                dep.add_dependent(task.id)
        # 将当前任务注册为其后续任务的前置（dependency）
        for existing in self.tasks.values():
            if task.id in existing.dependencies:
                task.add_dependent(existing.id)
        # 拓扑顺序已失效，清除缓存
        self._execution_order.clear()

    def get_task(self, task_id: str) -> Task | None:
        """根据 ID 获取任务，不存在时返回 None。"""
        return self.tasks.get(task_id)

    def all_tasks(self) -> list[Task]:
        """返回计划中所有任务的列表。"""
        return list(self.tasks.values())

    def root_tasks(self) -> list[Task]:
        """返回所有根任务（无前置依赖的任务）。"""
        return [task for task in self.tasks.values() if not task.dependencies]

    def executable_tasks(self) -> list[Task]:
        """返回当前所有可立即执行的任务（依赖已全部完成）。"""
        return [task for task in self.tasks.values() if task.is_executable(self.tasks)]

    def compute_execution_order(self) -> bool:
        """使用 DFS 计算任务的拓扑排序（执行顺序）。

        Returns:
            True 表示排序成功（无环）；False 表示检测到循环依赖。
        """
        order: list[str] = []
        visited: set[str] = set()       # 已完成排序的任务
        visiting: set[str] = set()      # 当前递归栈中的任务（用于环检测）

        def visit(task: Task) -> bool:
            """深度优先遍历单个任务及其依赖。"""
            if task.id in visiting:
                return False            # 发现环
            if task.id in visited:
                return True             # 已处理过，跳过
            visiting.add(task.id)
            for dep_id in task.dependencies:
                dep = self.tasks.get(dep_id)
                if dep and not visit(dep):
                    return False        # 依赖中存在环
            visiting.remove(task.id)
            visited.add(task.id)
            order.append(task.id)       # 依赖全部就绪后才加入顺序
            return True

        for task in self.tasks.values():
            if task.id not in visited and not visit(task):
                return False            # 存在循环依赖
        self._execution_order = order
        return True

    def execution_order(self) -> list[str]:
        """获取任务的拓扑执行顺序（惰性计算并缓存）。

        Returns:
            按依赖关系排序的任务 ID 列表。

        Raises:
            RuntimeError: 当检测到循环依赖时抛出。
        """
        if not self._execution_order:
            ok = self.compute_execution_order()
            if not ok:
                raise RuntimeError("无法获取拓扑顺序：检测到任务循环依赖")
        return list(self._execution_order)

    def execution_batches(self) -> list[list[Task]]:
        """将任务划分为可并行执行的批次。

        每个批次内的任务互不依赖，可以安全地并行执行；
        批次之间按依赖顺序排列。

        Returns:
            任务批次列表，每个批次是一个 Task 列表。
        """
        remaining = set(self.tasks)
        completed: set[str] = set()
        batches: list[list[Task]] = []
        while remaining:
            # 找出当前所有依赖已满足的任务作为一个批次
            batch_ids = [
                task_id
                for task_id in self.execution_order()
                if task_id in remaining
                and all(dep_id in completed for dep_id in self.tasks[task_id].dependencies)
            ]
            if not batch_ids:
                break                   # 无更多可调度任务（可能存在环）
            batch = [self.tasks[task_id] for task_id in batch_ids]
            batches.append(batch)
            completed.update(batch_ids)
            remaining.difference_update(batch_ids)
        return batches

    def progress(self) -> float:
        """计算计划完成进度（0.0 ~ 1.0）。"""
        if not self.tasks:
            return 1.0
        completed = sum(1 for task in self.tasks.values() if task.status == TaskStatus.COMPLETED)
        return completed / len(self.tasks)

    def is_all_completed(self) -> bool:
        """判断是否所有任务都已完成。"""
        return all(task.status == TaskStatus.COMPLETED for task in self.tasks.values())

    def has_failed(self) -> bool:
        """判断是否有任务执行失败。"""
        return any(task.status == TaskStatus.FAILED for task in self.tasks.values())

    def mark_started(self) -> None:
        """将计划标记为执行中，并记录开始时间。"""
        self.status = PlanStatus.RUNNING
        self.start_time = time.time()

    def mark_completed(self) -> None:
        """将计划标记为已完成，并记录结束时间。"""
        self.status = PlanStatus.COMPLETED
        self.end_time = time.time()

    def mark_failed(self) -> None:
        """将计划标记为失败，并记录结束时间。"""
        self.status = PlanStatus.FAILED
        self.end_time = time.time()

    def summarize(self) -> str:
        """生成计划的文本摘要，包含任务数、批次数及首末批次信息。"""
        batches = self.execution_batches()
        first_batch = ", ".join(task.id for task in batches[0]) if batches else "无"
        final_batch = ", ".join(task.id for task in batches[-1]) if batches else "无"
        return (
            f"计划 {self.id}：{self.summary or self.goal}\n"
            f"任务数：{len(self.tasks)} | 并行批次：{len(batches)} | "
            f"当前可执行：{len(self.executable_tasks())}\n"
            f"首批任务：{first_batch}\n"
            f"最终汇总：{final_batch}"
        )