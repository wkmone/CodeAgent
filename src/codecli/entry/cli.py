from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.console import Console

from codecli import __version__
from codecli.agent.agent import Agent
from codecli.agent.agent_runner import AgentRunner
from codecli.bootstrap import build_tool_registry
from codecli.config.config import load_config, CodeCliConfig
from codecli.entry.repl import PermissionModeController, _count_mcp_servers, _count_named_files, _approval_prompt, \
    _prompt_message, SLASH_COMMANDS, _permission_key_bindings, _handle_slash, _run_agent
from codecli.llm.factory import create_llm_client
from codecli.prompt.assembler import PromptAssembler
from codecli.render import RichRenderer
from codecli.skill.registry import SkillRegistry

app = typer.Typer(
    name="codecli",
    help="CodeCli - A command line interface for the CodeCli library.",
    invoke_without_command=True,
    no_args_is_help=False
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"codecli {__version__}")
        raise typer.Exit()


@app.callback()
def main(
        ctx: typer.Context,
        version: Annotated[
            bool,
            typer.Option("--version", callback=_version_callback, is_eager=True,
                         help="Show the version of the codecli library.")
        ] = False,
        # Workspace
        cwd: Annotated[
            Path | None,
            typer.Option("--cwd", help="Working directory (default: current dir)"),
        ] = None,
        prompt: Annotated[
            str | None,
            typer.Option("-p", "--prompt", help="Prompt for the workspace")
        ] = None,
        api_key: Annotated[
            str | None,
            typer.Option("--api-key", help="API key for the LLM")
        ] = None,
        model: Annotated[
            str | None,
            typer.Option("--model", help="LLM model")
        ] = None,
        provider: Annotated[
            str | None,
            typer.Option("--provider", help="LLM provider")
        ] = None,
        base_url: Annotated[
            str | None,
            typer.Option("--base-url", help="LLM base URL")
        ] = None,
        plain: Annotated[
            bool,
            typer.Option("--plain", help="Use plain text rendering (no rich formatting)"),
        ] = False,
        mode: Annotated[
            str | None,
            typer.Option("--mode", help="Agent mode: react, plan, team")
        ] = None,
        worker_mode: Annotated[
            str | None,
            typer.Option("--worker-mode", help="Sub-Agent worker mode in team runs: react or plan")
        ] = None,
        json_output: Annotated[
            bool | None,
            typer.Option("--json", help="Output in JSON format")
        ] = None,
) -> None:
    _ = version
    if ctx.invoked_subcommand is not None:
        return

    root = (cwd or Path.cwd()).resolve()

    configuration: dict = {}
    llm_configuration: dict = {}

    if api_key is not None:
        llm_configuration["api_key"] = api_key
    if model is not None:
        llm_configuration["model"] = model
    if provider is not None:
        llm_configuration["provider"] = provider
    if base_url is not None:
        llm_configuration["base_url"] = base_url
    if json_output is not None:
        llm_configuration["json_output"] = json_output
    if llm_configuration:
        configuration["llm"] = llm_configuration
    if plain:
        configuration["render_mode"] = "plain"

    config = load_config(project_root=root, configuration=configuration)
    if plain:
        config.render_mode = "plain"

    if prompt is not None:
        selected_mode = (mode or config.prompt.agent_mode or "react").lower()
        if selected_mode not in {"react", "plan", "team"}:
            raise typer.BadParameter("mode must be react, plan, or team", param_hint="--mode")
        if worker_mode not in {"react", "plan"}:
            raise typer.BadParameter(
                "worker-mode must be react or plan", param_hint="--worker-mode"
            )
        asyncio.run(
            _run_prompt(
                prompt,
                str(root),
                config,
                mode=selected_mode,
                worker_mode=worker_mode,
                json_output=json_output
            )
        )
    else:
        asyncio.run(start_repl(str(root), config))


async def _run_prompt(
        prompt: str,
        cwd: str,
        config,
        *,
        mode: str = "react",
        worker_mode: str = "react",
        json_output: bool = False,
) -> None:
    config.render_mode = "plain"
    if not config.llm.api_key:
        typer.echo(
            "Fatal error: LLM API key is not configured. Set it with --api-key, "
            "via the CODECLI_API_KEY environment variable, or in "
            "~/.codecli/config.json or .codecli/config.json.",
            err=True,
        )
        raise typer.Exit(1)

    registry, manager = await build_tool_registry(config=config, cwd=cwd)
    if manager and manager.last_errors:
        for name, error in manager.last_errors.items():
            typer.echo(f"MCP server {name} failed to load: {error}", err=True)

    runner = AgentRunner(
        llm_client=create_llm_client(config.llm),
        tool_registry=registry,
        config=config,
        cwd=cwd
    )
    try:
        if mode == "plan":
            result = await runner.plan_complete_async(prompt)
        elif mode == "team":
            result = await runner.team_complete_async(prompt, worker_mode=worker_mode)
        else:
            result = await runner.ask_complete_async(prompt)
    except Exception as exc:  # noqa: BLE001 - CLI should report model/config errors cleanly
        typer.echo(f"Fatal error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "text": result.text,
                    "mode": mode,
                    "worker_mode": worker_mode if mode == "team" else None,
                    "turns": result.turns,
                    "total_tokens": result.total_tokens,
                    "usage": result.usage.to_dict(),
                    "cost": result.cost,
                },
                ensure_ascii=False,
            )
        )
    else:
        typer.echo(result.text)

async def start_repl(cwd: str, config: CodeCliConfig) -> None:
    console = Console()
    permission_mode = PermissionModeController(config)
    registry, mcp_manager = await build_tool_registry(config=config, cwd=cwd)
    client = create_llm_client(config.llm)
    system_prompt = PromptAssembler(
        config=config,
        cwd=cwd,
        tool_names=registry.list_names(),
        model=client.model_name,
        provider=client.provider_name,
    ).build_static()
    tool_count = len(registry.list_names())
    mcp_server_count = _count_mcp_servers(mcp_manager)
    skill_count = len(SkillRegistry(cwd).list())
    agents_file_count = _count_named_files(cwd, "AGENTS.md")
    renderer = RichRenderer(context_window=client.max_context_window)
    renderer.banner(
        model=client.model_name,
        provider=client.provider_name,
        cwd=cwd,
        tools=tool_count,
        version=__version__,
        api_key_configured=bool(config.llm.api_key),
        mcp_servers=mcp_server_count,
        skills=skill_count,
        agents_files=agents_file_count,
        hitl_mode=config.policy.hitl_mode,
    )
    agent = Agent(
        llm_client=client,
        tool_registry=registry,
        system_prompt=system_prompt,
        cwd=cwd,
        config=config,
        approval_callback=lambda request: _approval_prompt(request, console, permission_mode),
    )

    history_path = Path.home() / ".codecli" / "history" / "prompt_history.txt"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    session = PromptSession(
        message=lambda: _prompt_message(
            cwd=cwd,
            model=agent.llm_client.model_name,
            tools=tool_count,
            agents_files=agents_file_count,
            mcp_servers=mcp_server_count,
            skills=skill_count,
            stats=renderer.toolbar_status(),
            permission_mode=permission_mode.mode,
        ),
        history=FileHistory(str(history_path)),
        completer=WordCompleter(SLASH_COMMANDS, ignore_case=True),
        placeholder=[("class:placeholder", "Type your message or @path/to/file")],
        style=Style.from_dict(
            {
                "prompt": "bold #ffffff bg:#262626",
                "placeholder": "#9a9a9a bg:#262626",
                "prompt.dim": "#a3a3a3 bg:#000000",
                "prompt.count.agents": "bold #22d3ee bg:#000000",
                "prompt.count.mcp": "bold #c084fc bg:#000000",
                "prompt.count.skills": "bold #facc15 bg:#000000",
                "prompt.tools": "bold #22d3ee bg:#000000",
                "toolbar.model": "noreverse bold #ffffff bg:#000000",
                "toolbar.ctx.bar": "noreverse #22c55e bg:#000000",
                "toolbar.ctx.value": "noreverse #ffffff bg:#000000",
                "toolbar.cwd.value": "noreverse #c084fc bg:#000000",
                "toolbar.mode.default": "noreverse bold #22c55e bg:#000000",
                "toolbar.mode.auto": "noreverse bold #f59e0b bg:#000000",
                "toolbar.gap": "noreverse #ffffff bg:#000000",
            }
        ),
        key_bindings=_permission_key_bindings(permission_mode),
    )

    while True:
        try:
            user_input = await session.prompt_async()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        message = user_input.strip()
        if not message:
            continue
        if message.startswith("/"):
            should_exit = await _handle_slash(
                message,
                console,
                cwd,
                config,
                agent,
                registry,
                permission_mode,
                renderer,
            )
            if should_exit:
                return
            continue
        await _run_agent(agent, renderer, message)



