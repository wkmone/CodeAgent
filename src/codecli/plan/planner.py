from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

from codecli.llm.base import LlmClient
from codecli.plan.models import ExecutionPlan, Task, TaskType
from codecli.types import Message, Usage

PLANNER_PROMPT = """你是 CodeCLI 的任务规划器。
你的职责：把用户目标拆解成可执行任务DAG。

### 重要硬性规则
1. **只输出纯JSON，禁止输出任何解释、思考、前置后置文字，不要markdown注释。**
2. 只允许最外层key：`summary`、`tasks`；**禁止使用steps字段**。
3. 输出JSON不要尾部多余逗号。
4. `dependencies`必须是数组；没有依赖写`[]`，不要写null。
5. `dependencies`内的每一个id，**必须是tasks数组中已经定义的任务id**，禁止引用不存在任务。
6. 不要生成循环依赖；不要自依赖。
7. summary、description使用和用户提问相同语言；用户输入中文，就输出中文内容。
8. JSON字段名、type枚举值必须为英文。

### TaskType枚举含义
- FILE_READ：读取、查看、打开文件
- FILE_WRITE：创建、修改、写入文件
- COMMAND：执行shell命令
- ANALYSIS：文本/代码分析、信息整理、推理
- VERIFICATION：校验、验证执行结果是否符合预期

### JSON输出格式
{
  "summary": "简短的计划摘要",
  "tasks": [
    {
      "id": "简短唯一标识符，英文/数字，不要中文",
      "description": "具体可执行的单步操作，不要把多件事情写在同一个任务",
      "type": "FILE_READ|FILE_WRITE|COMMAND|ANALYSIS|VERIFICATION",
      "dependencies": ["其他任务id"]
    }
  ]
}

> DAG规则：任务之间通过dependencies表达依赖关系；
> 两个任务互相没有dependencies，则代表二者可以并行执行。
"""


class Planner:
    def __init__(self, llm_client: LlmClient):
        self.llm_client = llm_client
        self.last_usage = Usage()

    async def create_plan(self, goal: str) -> ExecutionPlan:
        plan: ExecutionPlan | None = None
        async for event in self.stream_plan(goal):
            if event.get("type") == "plan_created":
                plan = event["plan"]
        if plan is None:
            raise ValueError("planner did not produce an execution plan")
        return plan

    async def stream_plan(self, goal: str) -> AsyncIterator[dict[str, Any]]:
        """Create a plan while preserving provider reasoning and usage events."""
        self.last_usage = Usage()
        if _is_simple_goal(goal):
            yield {"type": "plan_created", "plan": _minimal_plan(goal)}
            return

        text = ""
        messages = [Message(role="user", content=f"请为以下目标创建执行计划：\n{goal}")]
        async for event in self.llm_client.chat(messages, [], system_prompt=PLANNER_PROMPT):
            event_type = event.get("type")
            if event_type == "text_delta":
                # Planner text is machine-readable JSON. Keep it out of the user-facing
                # stream and expose the parsed plan below instead.
                text += str(event.get("text") or "")
            elif event_type == "thinking_delta":
                yield {
                    "type": "thinking_delta",
                    "thinking": str(event.get("thinking") or ""),
                    "phase": "planning",
                }
            elif event_type == "usage":
                usage = Usage.from_mapping(event.get("usage") or {})
                self.last_usage = self.last_usage + usage
                yield {"type": "usage", "usage": usage.to_dict(), "phase": "planning"}
            elif event_type == "error":
                err = event["error"]
                if isinstance(err, Exception):
                    raise err
                raise RuntimeError(f"LLM 规划错误: {err}")

        try:
            plan = self.parse_plan(goal, text)
        except Exception as e:
            raise RuntimeError(f"解析规划输出失败，原始输出: {repr(text)}")

        yield {"type": "plan_created", "plan": plan}

    async def replan(self, failed_plan: ExecutionPlan, failure_reason: str) -> ExecutionPlan:
        completed_tasks = "\n".join(
            f"- {task.id}: {task.description}"
            for task in failed_plan.all_tasks()
            if task.result and not task.error
        )

        failed_tasks = "\n".join(
            f"- {task.id}: {task.description}, error={task.error}"
            for task in failed_plan.all_tasks()
            if task.error
        )

        return await self.create_plan(
            f"{failed_plan.goal}\n"
            f"失败原因：{failure_reason}\n"
            f"✅已经成功完成任务（尽量不要重复执行）：\n{completed_tasks}\n"
            f"❌执行失败任务，请修正这些步骤：\n{failed_tasks}"
        )

    def parse_plan(self, goal: str, plan_json: str) -> ExecutionPlan:
        data = _parse_json_object(plan_json)

        if "tasks" in data:
            task_nodes = data["tasks"]
        elif "steps" in data:
            task_nodes = data["steps"]
        else:
            task_nodes = []

        if not isinstance(task_nodes, list) or not task_nodes:
            raise ValueError("planner output did not contain a non-empty tasks/steps array")

        plan = ExecutionPlan(id=f"plan_{int(time.time() * 1000)}", goal=goal)
        plan.summary = str(data.get("summary") or "")

        id_mapping: dict[str, str] = {}

        for index, node in enumerate(task_nodes, start=1):
            if not isinstance(node, dict):
                continue
            original_id = str(node.get("id") or f"auto_{index}")
            new_id = f"task_{index}"
            id_mapping[original_id] = new_id
            plan.add_task(
                Task(
                    id=new_id,
                    description=str(node.get("description") or original_id),
                    type=_parse_task_type(str(node.get("type") or "ANALYSIS")),
                )
            )

        for index, node in enumerate(task_nodes, start=1):
            if not isinstance(node, dict):
                continue
            task = plan.get_task(f"task_{index}")
            if not task:
                continue
            dependencies = node.get("dependencies") or []
            if not isinstance(dependencies, list):
                continue
            for raw_dep in dependencies:
                dep_id = id_mapping.get(str(raw_dep), str(raw_dep))
                if dep_id in plan.tasks:
                    task.add_dependency(dep_id)
                    plan.tasks[dep_id].add_dependent(task.id)

        if not plan.compute_execution_order():
            raise ValueError("plan contains a cyclic dependency")
        return plan


async def _collect_text(
        llm_client: LlmClient,
        messages: list[Message],
        *,
        system_prompt: str,
) -> str:
    text, _usage = await _collect_text_and_usage(llm_client, messages, system_prompt=system_prompt)
    return text


async def _collect_text_and_usage(
        llm_client: LlmClient,
        messages: list[Message],
        *,
        system_prompt: str,
) -> tuple[str, Usage]:
    text = ""
    usage = Usage()
    async for event in llm_client.chat(messages, [], system_prompt=system_prompt):
        event_type = event.get("type")
        if event_type == "text_delta":
            text += str(event.get("text") or "")
        elif event_type == "usage":
            usage = usage + Usage.from_mapping(event.get("usage") or {})
        elif event_type == "error":
            err = event["error"]
            if isinstance(err, Exception):
                raise err
            raise RuntimeError(f"LLM error: {err}")
    return text, usage


def _parse_json_object(text: str) -> dict[str, Any]:
    if not text:
        raise ValueError("empty planner output")
    cleaned = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()
    # 提取最外层大括号，过滤前后多余自然语言
    match = re.search(r"\{.*\}", cleaned, re.S)
    if match:
        cleaned = match.group(0)
    if not cleaned:
        raise ValueError("empty planner output after clean")
    obj = json.loads(cleaned)
    if not isinstance(obj, dict):
        raise ValueError(f"planner output is not json object, type={type(obj)}")
    return obj

def _parse_task_type(value: str) -> TaskType:
    normalized = value.upper()
    try:
        return TaskType(normalized)
    except ValueError:
        return TaskType.ANALYSIS


def _is_simple_goal(goal: str | None) -> bool:
    normalized = (goal or "").strip()
    if not normalized or len(normalized) > 30:
        return False
    multi_step_cues = ["然后", "并且", "再", "最后", "同时", "先", "之后", "接着", "以及"]
    if any(cue in normalized for cue in multi_step_cues):
        return False
    simple_cues = ["列出", "查看", "读取", "显示", "执行", "运行", "搜索", "当前目录", "文件"]
    return any(cue in normalized for cue in simple_cues)


def _minimal_plan(goal: str) -> ExecutionPlan:
    normalized = goal.strip()
    plan = ExecutionPlan(id=f"plan_{int(time.time() * 1000)}", goal=normalized)
    plan.summary = f"直接执行简单任务：{normalized}"
    plan.add_task(Task(id="task_1", description=normalized, type=_infer_simple_type(normalized)))
    plan.compute_execution_order()
    return plan


def _infer_simple_type(goal: str) -> TaskType:
    if any(token in goal for token in ["读取", "打开", "查看"]) and "文件" in goal:
        return TaskType.FILE_READ
    if any(token in goal for token in ["写入", "修改", "创建文件"]):
        return TaskType.FILE_WRITE
    if any(token in goal for token in ["分析", "总结", "解释"]):
        return TaskType.ANALYSIS
    if any(token in goal for token in ["验证", "检查"]):
        return TaskType.VERIFICATION
    return TaskType.COMMAND
