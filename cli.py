#!/usr/bin/env python3
"""qwe-qwe CLI — lightweight AI agent for local models."""

import sys, time, readline
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
import agent, db, soul, skills

console = Console()

# Enable readline for input history + arrow keys
readline.parse_and_bind('"\e[A": previous-history')
readline.parse_and_bind('"\e[B": next-history')
readline.parse_and_bind('"\e[C": forward-char')
readline.parse_and_bind('"\e[D": backward-char')

LOGO = """[bold yellow]
   ██████╗ ██╗    ██╗███████╗     ██████╗ ██╗    ██╗███████╗
  ██╔═══██╗██║    ██║██╔════╝    ██╔═══██╗██║    ██║██╔════╝
  ██║   ██║██║ █╗ ██║█████╗█████╗██║   ██║██║ █╗ ██║█████╗  
  ██║▄▄ ██║██║███╗██║██╔══╝╚════╝██║▄▄ ██║██║███╗██║██╔══╝  
  ╚██████╔╝╚███╔███╔╝███████╗    ╚██████╔╝╚███╔███╔╝███████╗
   ╚══▀▀═╝  ╚══╝╚══╝ ╚══════╝     ╚══▀▀═╝  ╚══╝╚══╝ ╚══════╝[/]"""


def _soul_bar_text() -> str:
    s = soul.load()
    traits = " ".join(f"{k}:{v}" for k, v in s.items() if k not in ("name", "language"))
    return f"⚡ {s['name']} | {s['language']} | {traits}"


def _status_line() -> str:
    s = soul.load()
    s_prompt = int(db.kv_get("session_prompt_tokens") or "0")
    s_compl = int(db.kv_get("session_completion_tokens") or "0")
    s_total = s_prompt + s_compl
    s_turns = db.kv_get("session_turns") or "0"
    active_skills = skills.get_active()
    sk = f" | skills: {','.join(sorted(active_skills))}" if active_skills else ""
    return (
        f"agent {s['name']} | {agent.config.LLM_MODEL} | "
        f"tokens {s_total:,} ({s_turns} turns){sk}"
    )


def show_banner():
    console.print(LOGO)
    console.print(
        "[dim]  lightweight offline AI agent • runs on your hardware[/]",
        justify="center",
    )
    console.print(
        "  [dim]/soul  /skills  /memory  /stats  /clear  /quit[/]\n"
    )


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
        f"[cyan]Memory:[/]      Qdrant ({agent.config.QDRANT_MODE}, {__import__('memory').count()} points)\n"
        f"[cyan]Database:[/]    {agent.config.DB_PATH}",
        title="[bold]📊 Session Stats[/]",
        border_style="cyan",
        padding=(0, 2),
    ))


def _render_slider(value: int, width: int = 20) -> str:
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
        return

    # Interactive mode
    console.print()
    console.print("  [bold magenta]🧬 Soul Editor[/]")
    console.print("  [dim]Enter value or press Enter to keep current. q to finish.[/]\n")

    for field in ("name", "language"):
        try:
            current = s[field]
            new_val = console.input(f"  [cyan]{field}[/] [dim]({current})[/] > ").strip()
            if new_val:
                soul.save(field, new_val)
                s[field] = new_val
        except (EOFError, KeyboardInterrupt):
            return

    console.print()
    numeric_traits = [k for k in s if k not in ("name", "language")]
    for trait in numeric_traits:
        value = s[trait]
        low, high = soul.TRAIT_DESCRIPTIONS.get(trait, ("low", "high"))
        bar = "█" * value + "░" * (10 - value)
        console.print(f"  [{bar}] [bold cyan]{trait}[/] = [bold]{value}[/]  [dim]{low} ← → {high}[/]")
        try:
            inp = console.input(f"  [dim]new value 0-10 (enter=keep)[/] > ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if inp == "q":
            break
        if inp.isdigit():
            v = max(0, min(10, int(inp)))
            soul.save(trait, v)
            s[trait] = v

    console.print()
    console.print(Panel(
        soul.format_display(soul.load()),
        title="[bold]🧬 Soul — saved[/]",
        border_style="magenta",
        padding=(0, 2),
    ))


def handle_skills_command(args: str):
    if args:
        parts = args.split(maxsplit=1)
        if len(parts) == 2:
            action, name = parts
            if action == "on":
                console.print(f"  [green]{skills.enable(name)}[/]")
            elif action == "off":
                console.print(f"  [yellow]{skills.disable(name)}[/]")
        return

    # Interactive skill selector
    import readchar

    all_skills = skills.list_all()
    if not all_skills:
        console.print("  [dim]No skills found in skills/ folder.[/]")
        return

    selected = {s["name"] for s in all_skills if s["active"]}
    cursor = 0

    def render():
        # Clear previous render
        if render.first:
            render.first = False
        else:
            # Move up and clear lines
            lines_to_clear = len(all_skills) + 3
            sys.stdout.write(f"\033[{lines_to_clear}A\033[J")

        console.print("  [bold magenta]⚡ Skills[/]  [dim]↑↓ move  space toggle  enter save[/]\n")
        for i, s in enumerate(all_skills):
            check = "[green]●[/]" if s["name"] in selected else "[dim]○[/]"
            pointer = "[bold yellow]▸[/]" if i == cursor else " "
            name_fmt = f"[bold]{s['name']}[/]" if s["name"] in selected else s["name"]
            tools_count = f"[dim]({s['tools']} tools)[/]"
            desc = f"[dim]— {s['description']}[/]" if s["description"] else ""
            console.print(f"  {pointer} {check} {name_fmt} {tools_count} {desc}")
        console.print()

    render.first = True
    render()

    while True:
        key = readchar.readkey()

        if key in (readchar.key.UP, "k"):
            cursor = (cursor - 1) % len(all_skills)
        elif key in (readchar.key.DOWN, "j"):
            cursor = (cursor + 1) % len(all_skills)
        elif key == " ":
            name = all_skills[cursor]["name"]
            if name in selected:
                selected.discard(name)
            else:
                selected.add(name)
        elif key in (readchar.key.ENTER, "\r", "\n"):
            # Save
            skills.set_active(selected)
            console.print(f"  [green]✓ Active: {', '.join(sorted(selected)) or 'none'}[/]")
            return
        elif key in ("q", readchar.key.ESC):
            return

        render()


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
            # Status line + input separator
            console.print(f"  [dim]{_status_line()}[/]")
            console.print("  [dim]" + "─" * (console.width - 4) + "[/]")
            user_input = input("  ⚡ > ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n  [dim]👋[/]")
            break

        if not user_input:
            continue
        if user_input == "/quit":
            console.print("  [dim]👋[/]")
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

        with console.status("[yellow]  ...[/]", spinner="dots"):
            try:
                result = agent.run(user_input)
            except Exception as e:
                console.print(f"  [red]✗ {e}[/]")
                continue

        console.print()
        console.print(f"  🦆 ", end="")
        console.print(Markdown(result.reply))
        console.print()


if __name__ == "__main__":
    main()
