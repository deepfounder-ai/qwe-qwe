#!/usr/bin/env python3
"""qwe-qwe CLI — lightweight AI agent for local models."""

import sys, time, shutil
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
import agent, db, soul, skills

console = Console()


def _soul_status_bar():
    """Render persistent soul status bar at bottom of terminal."""
    s = soul.load()
    width = shutil.get_terminal_size().columns
    traits = " ".join(f"{k}:{v}" for k, v in s.items() if k not in ("name", "language"))
    bar = f" ⚡ {s['name']} | {s['language']} | {traits} "
    padded = bar.ljust(width)
    # Move to last line, print, move back
    print(f"\033[s\033[{shutil.get_terminal_size().lines};0H\033[7m{padded}\033[0m\033[u", end="", flush=True)

LOGO = """[bold yellow]
   ██████╗ ██╗    ██╗███████╗     ██████╗ ██╗    ██╗███████╗
  ██╔═══██╗██║    ██║██╔════╝    ██╔═══██╗██║    ██║██╔════╝
  ██║   ██║██║ █╗ ██║█████╗█████╗██║   ██║██║ █╗ ██║█████╗  
  ██║▄▄ ██║██║███╗██║██╔══╝╚════╝██║▄▄ ██║██║███╗██║██╔══╝  
  ╚██████╔╝╚███╔███╔╝███████╗    ╚██████╔╝╚███╔███╔╝███████╗
   ╚══▀▀═╝  ╚══╝╚══╝ ╚══════╝     ╚══▀▀═╝  ╚══╝╚══╝ ╚══════╝[/]"""

COMMANDS = {
    "/soul": "Personality",
    "/skills": "Manage skills",
    "/memory": "Search memories",
    "/stats": "Stats",
    "/clear": "Reset",
    "/quit": "Exit",
}


def show_banner():
    console.print(LOGO)
    console.print(
        "[dim]  lightweight offline AI agent • runs on your hardware[/]\n",
        justify="center",
    )
    cols = "  ".join(f"[bold cyan]{k}[/][dim] {v}[/]" for k, v in COMMANDS.items())
    console.print(f"  {cols}\n")

    # Reserve last line for status bar
    print(f"\033[1;{shutil.get_terminal_size().lines - 1}r", end="", flush=True)
    _soul_status_bar()


def show_stats():
    s_prompt = db.kv_get("session_prompt_tokens") or "0"
    s_compl = db.kv_get("session_completion_tokens") or "0"
    s_turns = db.kv_get("session_turns") or "0"
    s_total = int(s_prompt) + int(s_compl)
    s = soul.load()
    console.print(Panel(
        f"[cyan]Agent:[/]       {s['name']}\n"
        f"[cyan]Turns:[/]       {s_turns}\n"
        f"[cyan]Tokens:[/]      ↑{s_prompt} prompt  ↓{s_compl} completion  Σ{s_total} total\n"
        f"[cyan]Model:[/]       {agent.config.LLM_MODEL}\n"
        f"[cyan]Memory:[/]      Qdrant ({agent.config.QDRANT_MODE})\n"
        f"[cyan]Database:[/]    {agent.config.DB_PATH}",
        title="[bold]📊 Session Stats[/]",
        border_style="cyan",
        padding=(0, 2),
    ))


def _render_slider(value: int, width: int = 20) -> str:
    """Render a visual slider: ───●──────"""
    pos = int(value / 10 * (width - 1))
    bar = "─" * pos + "[bold yellow]●[/]" + "─" * (width - 1 - pos)
    return bar


def handle_soul_command(args: str):
    s = soul.load()
    if args:
        parts = args.split(maxsplit=1)
        if len(parts) == 2:
            key, value = parts
            try:
                value_int = int(value)
                if 0 <= value_int <= 10:
                    value = value_int
                else:
                    console.print("  [red]Value must be 0-10[/]")
                    return
            except ValueError:
                pass
            result = soul.save(key, value)
            console.print(f"  [magenta]{result}[/]")
        _soul_status_bar()
        return

    # Interactive mode
    console.print(Panel(
        "[bold]🧬 Soul Editor[/]\n"
        "[dim]← → adjust  │  Enter confirm  │  q done[/]",
        border_style="magenta",
        padding=(0, 2),
    ))

    # Edit name and language first
    for field in ("name", "language"):
        try:
            current = s[field]
            new_val = console.input(f"  [cyan]{field}[/] [dim]({current})[/] > ").strip()
            if new_val:
                soul.save(field, new_val)
                s[field] = new_val
        except (EOFError, KeyboardInterrupt):
            return

    # Interactive sliders for numeric traits
    numeric_traits = [k for k in s if k not in ("name", "language")]

    for trait in numeric_traits:
        value = s[trait]
        low, high = soul.TRAIT_DESCRIPTIONS.get(trait, ("low", "high"))

        while True:
            slider = _render_slider(value)
            console.print(
                f"\r  [dim]{low[:12]:>12s}[/] {slider} [dim]{high[:12]:<12s}[/]  "
                f"[bold cyan]{trait}[/] = [bold yellow]{value:>2d}[/]/10",
                end="",
            )
            try:
                key = console.input("  [dim](←-/+→/enter)[/] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                console.print()
                return

            if key in ("-", "a", "[", ","):
                value = max(0, value - 1)
            elif key in ("+", "d", "]", ".", "="):
                value = min(10, value + 1)
            elif key.isdigit():
                v = int(key)
                if v == 1 and value == 1:
                    value = 10
                else:
                    value = min(v, 10)
                soul.save(trait, value)
                s[trait] = value
                break
            elif key == "":
                soul.save(trait, value)
                s[trait] = value
                break
            elif key == "q":
                soul.save(trait, value)
                console.print()
                console.print(Panel(
                    soul.format_display(soul.load()),
                    title="[bold]🧬 Soul — saved[/]",
                    border_style="magenta",
                    padding=(0, 2),
                ))
                _soul_status_bar()
                return

    # Show final result
    console.print()
    console.print(Panel(
        soul.format_display(soul.load()),
        title="[bold]🧬 Soul — saved[/]",
        border_style="magenta",
        padding=(0, 2),
    ))
    _soul_status_bar()


def handle_skills_command(args: str):
    if not args:
        all_skills = skills.list_all()
        if not all_skills:
            console.print("  [dim]No skills found in skills/ folder.[/]")
            return
        for s in all_skills:
            status = "[green]●[/]" if s["active"] else "[dim]○[/]"
            tools_count = f"[dim]({s['tools']} tools)[/]"
            desc = f"[dim]— {s['description']}[/]" if s["description"] else ""
            console.print(f"  {status} [bold]{s['name']}[/] {tools_count} {desc}")
        console.print("\n  [dim]/skills on <name>  |  /skills off <name>[/]")
        return

    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        console.print("  [dim]Usage: /skills on <name>  |  /skills off <name>[/]")
        return

    action, name = parts
    if action == "on":
        console.print(f"  [green]{skills.enable(name)}[/]")
    elif action == "off":
        console.print(f"  [yellow]{skills.disable(name)}[/]")
    else:
        console.print("  [dim]Usage: /skills on <name>  |  /skills off <name>[/]")


def search_memory():
    query = console.input("[cyan]  search query >[/] ").strip()
    if not query:
        return
    import memory as mem
    results = mem.search(query, limit=5)
    if not results:
        console.print("  [dim]No memories found.[/]")
        return
    for r in results:
        score_color = "green" if r["score"] > 0.7 else "yellow" if r["score"] > 0.5 else "dim"
        console.print(
            f"  [{score_color}]●[/] [{score_color}]{r['score']}[/] "
            f"[bold]{r['tag']}[/] → {r['text']}"
        )


def main():
    show_banner()

    while True:
        try:
            user_input = console.input("[bold green]  ⚡ >[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\033[r", end="", flush=True)
            console.print("\n  [dim]👋 bye[/]")
            break

        if not user_input:
            continue

        if user_input == "/quit":
            print("\033[r", end="", flush=True)  # reset scroll region
            console.print("  [dim]👋 bye[/]")
            break
        if user_input == "/clear":
            db.clear_history()
            console.print("  [yellow]✓ History cleared.[/]")
            continue
        if user_input == "/stats":
            show_stats()
            continue
        if user_input.startswith("/soul"):
            handle_soul_command(user_input[5:].strip())
            continue
        if user_input.startswith("/skills"):
            handle_skills_command(user_input[7:].strip())
            continue
        if user_input == "/memory":
            search_memory()
            continue

        if user_input.startswith("/"):
            console.print(f"  [dim]Unknown command: {user_input.split()[0]}[/]")
            continue

        t0 = time.time()

        with console.status("[yellow]  thinking...[/]", spinner="dots"):
            try:
                result = agent.run(user_input)
            except Exception as e:
                console.print(f"  [red]✗ {e}[/]")
                continue

        elapsed = time.time() - t0

        # Build footer
        parts = [f"{elapsed:.1f}s"]
        parts.append(f"↑{result.prompt_tokens} ↓{result.completion_tokens}")
        session_total = int(db.kv_get("session_prompt_tokens") or "0") + \
                        int(db.kv_get("session_completion_tokens") or "0")
        parts.append(f"Σ{session_total}")
        if result.auto_context_hits:
            parts.append(f"📎{result.auto_context_hits}")
        if result.tool_calls_made:
            parts.append(f"🔧 {', '.join(result.tool_calls_made)}")
        footer = " │ ".join(parts)

        console.print()
        console.print(Panel(
            Markdown(result.reply),
            border_style="yellow",
            padding=(0, 2),
            subtitle=f"[dim]{footer}[/]",
            subtitle_align="right",
        ))


if __name__ == "__main__":
    main()
