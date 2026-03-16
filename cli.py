#!/usr/bin/env python3
"""qwe-qwe CLI — lightweight AI agent for local models."""

import sys, time, readline
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
import agent, config, db, soul, skills, tasks, scheduler, providers, threads
import logger

_log = logger.get("cli")

console = Console()

# Enable readline for input history + arrow keys
readline.parse_and_bind('"\\e[A": previous-history')
readline.parse_and_bind('"\\e[B": next-history')
readline.parse_and_bind('"\\e[C": forward-char')
readline.parse_and_bind('"\\e[D": backward-char')

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
    prov = providers.get_active_name()
    model = providers.get_model()
    tid = threads.get_active_id()
    t = threads.get(tid)
    thread_name = t["name"] if t else tid
    return (
        f"agent {s['name']} | {model} @ {prov} | "
        f"💬 {thread_name} | tokens {s_total:,} ({s_turns} turns){sk}"
    )


def show_banner():
    console.print(LOGO)
    s = soul.load()
    user_name = db.kv_get("user_name") or "Boss"
    city = db.kv_get("timezone_city") or "somewhere"
    mem_count = 0
    try:
        import memory
        mem_count = memory.count()
    except Exception:
        pass
    active = skills.get_active()

    console.print(f"""
  [dim]🦆 qwe-qwe — your fully offline AI agent[/]
  [dim]No cloud. No API keys. No subscriptions. Just your GPU.[/]
  
  [dim]🧠 Model:[/]  {providers.get_model()} @ {providers.get_active_name()}
  [dim]👤 User:[/]   {user_name} [dim]({city}, UTC{config.TZ_OFFSET:+d})[/]
  [dim]🤖 Agent:[/]  {s['name']} [dim]| {s['language']}[/]
  [dim]💾 Memory:[/] {mem_count} memories [dim]| SQLite + Qdrant[/]
  [dim]⚙️  Skills:[/] {', '.join(sorted(active)) if active else 'none'} [dim]| /skills to manage[/]
  
  [dim]Commands: /thread  /model  /provider  /telegram  /soul  /skills  /memory  /cron  /tasks  /stats  /logs  /clear  /quit[/]
"""
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
        f"[cyan]Model:[/]       {providers.get_model()} ({providers.get_active_name()})\n"
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
            value = value.lower().strip()
            if value not in ("low", "moderate", "high") and key not in ("name", "language"):
                console.print("  [red]Value must be low, moderate, or high[/]")
                return
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
    level_traits = [k for k in s if k not in ("name", "language")]
    for trait in level_traits:
        level = s[trait]
        low, high = soul.TRAIT_DESCRIPTIONS.get(trait, ("low", "high"))
        console.print(f"  [bold cyan]{trait}[/] = [bold]{level}[/]  [dim]{low} ↔ {high}[/]")
        try:
            inp = console.input(f"  [dim]low/moderate/high (enter=keep)[/] > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if inp == "q":
            break
        if inp in ("low", "moderate", "high"):
            soul.save(trait, inp)
            s[trait] = inp

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


def handle_cron(args: str):
    if args.startswith("rm "):
        try:
            task_id = int(args[3:].strip())
            console.print(f"  {scheduler.remove(task_id)}")
        except ValueError:
            console.print("  [dim]Usage: /cron rm <id>[/]")
        return

    tasks_list = scheduler.list_tasks()
    if not tasks_list:
        console.print("  [dim]No scheduled tasks. Agent can create them with schedule_task tool.[/]")
        return
    for t in tasks_list:
        status = "[green]●[/]" if t["enabled"] else "[dim]○[/]"
        repeat = "🔄" if t["repeat"] else "⏱"
        console.print(f"  {status} #{t['id']} {repeat} [bold]{t['name']}[/] → {t['next_run']} [dim]({t['schedule']})[/]")
        console.print(f"      [dim]{t['task'][:80]}[/]")
    console.print(f"\n  [dim]/cron rm <id> to remove[/]")


def show_tasks():
    results = tasks.get_results(clear=False)
    pending = tasks.pending_count()
    if not results and pending == 0:
        console.print("  [dim]No tasks.[/]")
        return
    if pending:
        console.print(f"  [yellow]⏳ {pending} task(s) running...[/]")
    for r in results:
        icon = "[green]✅[/]" if r["status"] == "done" else "[red]❌[/]"
        console.print(f"  {icon} #{r['id']} {r['task'][:50]}")
        console.print(f"      [dim]{r['result'][:100]}[/]")


def _check_background_tasks():
    """Show completed background tasks."""
    results = tasks.get_results(clear=True)
    for r in results:
        icon = "✅" if r["status"] == "done" else "❌"
        console.print(f"\n  [bold]{icon} Task #{r['id']}:[/] {r['task'][:60]}")
        console.print(f"  [dim]{r['result'][:200]}[/]\n")


def handle_thread(args: str):
    """Thread management.
    /thread              — list all threads
    /thread new <name>   — create new thread and switch to it
    /thread <id>         — switch to thread
    /thread rename <name> — rename current thread
    /thread archive      — archive current thread
    /thread delete <id>  — delete a thread
    """
    if not args:
        # List threads
        all_t = threads.list_all()
        if not all_t:
            console.print("  [dim]No threads yet.[/]")
            return

        console.print(f"\n  [bold]💬 Threads:[/]\n")
        for t in all_t:
            marker = "[green]●[/]" if t["active"] else "[dim]○[/]"
            msgs = f"[dim]({t['messages']} msgs)[/]"
            preview = f" [dim]— {t['preview']}[/]" if t["preview"] else ""
            console.print(f"    {marker} [bold]{t['id']}[/] {t['name']} {msgs}{preview}")
        console.print(f"\n  [dim]/thread new <name>  /thread <id>  /thread rename <name>  /thread archive[/]\n")
        return

    parts = args.split(maxsplit=1)
    cmd = parts[0]

    # /thread new <name>
    if cmd == "new":
        name = parts[1] if len(parts) > 1 else f"Thread {len(threads.list_all()) + 1}"
        t = threads.create(name)
        threads.switch(t["id"])
        console.print(f"  [green]✓ Created & switched to '{name}' ({t['id']})[/]")
        return

    # /thread rename <name>
    if cmd == "rename":
        if len(parts) < 2:
            console.print("  [dim]Usage: /thread rename <new name>[/]")
            return
        result = threads.rename(threads.get_active_id(), parts[1])
        console.print(f"  [green]{result}[/]")
        return

    # /thread archive
    if cmd == "archive":
        tid = parts[1] if len(parts) > 1 else threads.get_active_id()
        result = threads.archive(tid)
        console.print(f"  [yellow]{result}[/]")
        return

    # /thread delete <id>
    if cmd == "delete":
        if len(parts) < 2:
            console.print("  [dim]Usage: /thread delete <id>[/]")
            return
        tid = parts[1]
        console.print(f"  [yellow]Delete thread {tid} and all messages? (y/n)[/]")
        try:
            confirm = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if confirm == "y":
            result = threads.delete(tid)
            console.print(f"  [red]{result}[/]")
        return

    # /thread <id> — switch
    result = threads.switch(cmd)
    if result.startswith("✓"):
        console.print(f"  [green]{result}[/]")
    else:
        console.print(f"  [red]{result}[/]")


def handle_model(args: str):
    """Switch model or list available. Usage: /model [name] or /model list"""
    if not args or args == "list":
        # Show current + fetch available
        current = providers.get_model()
        prov_name = providers.get_active_name()
        console.print(f"\n  [bold]🧠 Current model:[/] [cyan]{current}[/] [dim]({prov_name})[/]\n")

        console.print(f"  [dim]Fetching models from {prov_name}...[/]")
        models = providers.fetch_models()
        if models:
            console.print(f"  [bold]Available ({len(models)}):[/]")
            for m in models:
                marker = "[green]●[/]" if m == current else "[dim]○[/]"
                console.print(f"    {marker} {m}")
        else:
            # Show preset models
            p = providers.get_provider()
            if p.get("models"):
                console.print(f"  [bold]Preset models:[/]")
                for m in p["models"]:
                    marker = "[green]●[/]" if m == current else "[dim]○[/]"
                    console.print(f"    {marker} {m}")
            else:
                console.print(f"  [dim]No models found. Is the server running?[/]")
        console.print(f"\n  [dim]Usage: /model <name>[/]\n")
        return

    result = providers.set_model(args)
    console.print(f"  [green]{result}[/]")


def handle_provider(args: str):
    """Manage providers. Usage: /provider [name] [key <key>] [url <url>]"""
    if not args or args == "list":
        # List all providers
        all_p = providers.list_all()
        console.print(f"\n  [bold]🔌 Providers:[/]\n")
        for p in all_p:
            marker = "[green]●[/]" if p["active"] else "[dim]○[/]"
            key_status = "[green]🔑[/]" if p["has_key"] else "[dim]🔒[/]"
            model_count = f"[dim]({len(p['models'])} models)[/]" if p["models"] else ""
            console.print(f"    {marker} [bold]{p['name']}[/] {key_status} {model_count}")
            console.print(f"      [dim]{p['url']}[/]")
        console.print(f"\n  [dim]Usage:[/]")
        console.print(f"    [dim]/provider <name>              — switch to provider[/]")
        console.print(f"    [dim]/provider <name> key <key>    — set API key[/]")
        console.print(f"    [dim]/provider add <name> <url>    — add custom provider[/]")
        console.print()
        return

    parts = args.split()

    # /provider add <name> <url> [key]
    if parts[0] == "add" and len(parts) >= 3:
        name = parts[1]
        url = parts[2]
        key = parts[3] if len(parts) > 3 else ""
        result = providers.add(name, url, key)
        console.print(f"  [green]{result}[/]")
        return

    # /provider <name> key <key>
    if len(parts) >= 3 and parts[1] == "key":
        name = parts[0]
        key = parts[2]
        result = providers.set_key(name, key)
        console.print(f"  [green]{result}[/]")
        return

    # /provider <name> — switch
    name = parts[0]
    result = providers.switch(name)
    if result.startswith("✓"):
        console.print(f"  [green]{result}[/]")
        # Show available models
        models = providers.fetch_models(name)
        if models:
            console.print(f"  [dim]Models: {', '.join(models[:8])}{'...' if len(models) > 8 else ''}[/]")
    else:
        console.print(f"  [red]{result}[/]")


def show_logs(args: str):
    """Show recent logs. Usage: /logs [errors] [N]"""
    parts = args.split()
    log_file = "errors.log" if "errors" in parts else "qwe-qwe.log"
    n = 20
    for p in parts:
        if p.isdigit():
            n = int(p)

    log_path = Path(__file__).parent / "logs" / log_file
    if not log_path.exists():
        console.print("  [dim]No logs yet.[/]")
        return

    lines = log_path.read_text().splitlines()
    tail = lines[-n:]
    console.print(f"  [bold]📋 {log_file}[/] [dim](last {len(tail)} lines)[/]\n")
    for line in tail:
        if "| ERROR" in line or "| WARNING" in line:
            console.print(f"  [red]{line}[/]")
        elif "| EVENT" in line:
            console.print(f"  [cyan]{line}[/]")
        else:
            console.print(f"  [dim]{line}[/]")
    console.print()


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


_cron_results: list[tuple] = []

def _on_cron_complete(name, task, result):
    _cron_results.append((name, task, result))
    # Push notification immediately to terminal
    print(f"\n\n  ⏰ Cron '{name}': {task[:60]}")
    print(f"  {result[:200]}\n")
    sys.stdout.flush()

_CITY_TZ = {
    "moscow": 3, "london": 0, "berlin": 1, "paris": 1,
    "new york": -5, "los angeles": -8, "chicago": -6,
    "tokyo": 9, "sydney": 11, "dubai": 4, "mumbai": 5,
    "beijing": 8, "singapore": 8, "istanbul": 3,
    "buenos aires": -3, "são paulo": -3, "sao paulo": -3,
    "mexico city": -6, "bogota": -5, "lima": -5,
    "bangkok": 7, "seoul": 9, "jakarta": 7,
    "cairo": 2, "nairobi": 3, "lagos": 1,
    "amsterdam": 1, "madrid": 1, "rome": 1,
    "warsaw": 1, "kyiv": 2, "tbilisi": 4,
}


def _first_run_setup():
    """Interactive setup on first launch — city, name, language, soul."""
    console.print("\n  [bold yellow]⚡ Welcome to qwe-qwe! Let's set up.[/]\n")

    # 1. Auto-detect timezone
    try:
        import time as _time
        offset = -(_time.timezone // 3600) if _time.daylight == 0 else -(_time.altzone // 3600)
        config.TZ_OFFSET = offset
        db.kv_set("timezone", str(offset))
        tz_name = _time.tzname[0]
        db.kv_set("timezone_name", tz_name)
        console.print(f"  [green]✓[/] Timezone: UTC{'+' if offset >= 0 else ''}{offset} ({tz_name})")
    except Exception:
        config.TZ_OFFSET = 0

    # 2. User's name
    console.print("\n  [yellow]👤 What should I call you?[/] [dim](default: Boss)[/]")
    user_name = input("  Your name: ").strip()
    db.kv_set("user_name", user_name or "Boss")

    # 3. Agent name
    console.print("\n  [yellow]🤖 What should your agent be called?[/] [dim](default: Agent)[/]")
    name = input("  Name: ").strip()
    if name:
        db.kv_set("soul:name", name)

    # 3. Language
    console.print("\n  [yellow]🗣 What language should it speak?[/] [dim](default: English)[/]")
    lang = input("  Language: ").strip()
    if lang:
        db.kv_set("soul:language", lang)

    # 4. Quick personality
    console.print("\n  [yellow]✨ Quick personality setup (0-10, Enter to skip):[/]")
    quick_traits = [
        ("humor", "Humor", "serious ↔ funny"),
        ("honesty", "Honesty", "diplomatic ↔ brutally direct"),
        ("brevity", "Brevity", "verbose ↔ concise"),
        ("formality", "Formality", "casual ↔ formal"),
        ("creativity", "Creativity", "practical ↔ unconventional"),
    ]
    for key, label, desc in quick_traits:
        try:
            val = input(f"  {label} ({desc}): ").strip()
            if val and val.isdigit():
                v = max(0, min(10, int(val)))
                db.kv_set(f"soul:{key}", str(v))
        except EOFError:
            break

    db.kv_set("setup_complete", "1")
    console.print("\n  [green]✓ Setup complete! Use /soul to tweak later.[/]\n")


def main():
    # Check first run
    if not db.kv_get("setup_complete"):
        _first_run_setup()
    else:
        # Load timezone from DB
        tz_val = db.kv_get("timezone")
        if tz_val is not None:
            config.TZ_OFFSET = int(tz_val)

    # Start scheduler
    scheduler.on_complete(_on_cron_complete)
    scheduler.start()
    console.print(f"  [dim]⏰ Scheduler running (UTC{config.TZ_OFFSET:+d})[/]")

    show_banner()
    _log.info("session started | model=%s | user=%s", config.LLM_MODEL, db.kv_get("user_name") or "Boss")

    while True:
        try:
            # Check background + scheduled tasks
            _check_background_tasks()
            while _cron_results:
                name, task, result = _cron_results.pop(0)
                console.print(f"\n  [bold]⏰ Cron '{name}':[/] {task[:60]}")
                console.print(f"  [dim]{result[:200]}[/]\n")
            # Status line + input separator
            console.print(f"  [dim]{_status_line()}[/]")
            console.print("  [dim]" + "─" * (console.width - 4) + "[/]")
            user_input = input("  ⚡ > ").strip()
        except (EOFError, KeyboardInterrupt):
            _log.info("session ended (user exit)")
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
        if user_input == "/tasks":
            show_tasks()
            continue
        if user_input.startswith("/cron"):
            handle_cron(user_input[5:].strip())
            continue
        if user_input == "/memory":
            search_memory()
            continue
        if user_input.startswith("/logs"):
            show_logs(user_input[5:].strip())
            continue
        if user_input.startswith("/thread"):
            handle_thread(user_input[7:].strip())
            continue
        if user_input.startswith("/model"):
            handle_model(user_input[6:].strip())
            continue
        if user_input.startswith("/telegram"):
            handle_telegram(user_input[9:].strip())
            continue
        if user_input.startswith("/provider"):
            handle_provider(user_input[9:].strip())
            continue
        if user_input == "/thinking":
            current = db.kv_get("thinking_enabled") == "true"
            new_val = not current
            db.kv_set("thinking_enabled", str(new_val).lower())
            console.print(f"  [yellow]💭 thinking: {'ON' if new_val else 'OFF'}[/]")
            continue
        if user_input.startswith("/"):
            console.print(f"  [dim]Unknown command: {user_input.split()[0]}[/]")
            continue

        try:
            result = agent.run(user_input)
        except Exception as e:
            _log.error(f"agent.run crashed: {e}", exc_info=True)
            console.print(f"  [red]✗ {str(e).replace('[', '(').replace(']', ')')}[/]")
            continue

        console.print()
        console.print(f"  🦆 ", end="")
        console.print(Markdown(result.reply))
        console.print()


def handle_telegram(args: str):
    """Handle /telegram commands."""
    import telegram_bot

    parts = args.split(maxsplit=1)
    cmd = parts[0] if parts else ""
    val = parts[1] if len(parts) > 1 else ""

    if not cmd or cmd == "status":
        s = telegram_bot.status()
        console.print(f"\n  [bold yellow]Telegram Bot[/]")
        console.print(f"  Token:    {'✓ set' if s['has_token'] else '✗ not set'}")
        console.print(f"  Enabled:  {'✓' if s['enabled'] else '✗'}")
        console.print(f"  Running:  {'✓' if s['running'] else '✗'}")
        console.print(f"  Bot:      @{s['username'] or '—'}")
        console.print(f"  Owner:    {'@' + s['owner_username'] if s['verified'] else 'not verified'}")
        console.print(f"  Groups:   {s['allowed_groups'] or 'any'}")
        console.print(f"  Mode:     {s['group_mode']}")
        console.print(f"  Topics:   {'on' if s['topics_enabled'] else 'off'}")
        if s.get('has_pending_code'):
            console.print(f"\n  [yellow]⏳ Activation code pending — send it to the bot in Telegram[/]")
        console.print(f"\n  [dim]Commands: /telegram token <TOKEN> | activate | start | stop | reset[/]\n")
        return

    if cmd == "token":
        if not val:
            console.print("  [red]Usage: /telegram token <BOT_TOKEN>[/]")
            return
        telegram_bot.set_token(val)
        me = telegram_bot.get_me(val)
        if me:
            console.print(f"  [green]✓ Token saved — @{me.get('username')}[/]")
            console.print(f"  [dim]Next: /telegram start → /telegram activate[/]")
        else:
            console.print(f"  [red]✗ Invalid token[/]")
        return

    if cmd == "activate":
        if telegram_bot.is_verified():
            console.print(f"  [yellow]Already verified as @{telegram_bot.get_owner_username()}[/]")
            console.print(f"  [dim]Run /telegram reset first to re-verify[/]")
            return
        if not telegram_bot.get_token():
            console.print(f"  [red]Set bot token first: /telegram token <TOKEN>[/]")
            return
        code = telegram_bot.generate_activation_code()
        console.print(f"\n  [bold green]🔑 Activation code: {code}[/]")
        console.print(f"  [dim]Send this code to your bot in Telegram within 10 minutes.[/]")
        console.print(f"  [dim]⚠️  3 wrong attempts = permanent ban for that Telegram user.[/]\n")
        return

    if cmd == "start":
        telegram_bot.set_enabled(True)
        from server import _telegram_handler
        telegram_bot.start(on_message=_telegram_handler)
        console.print(f"  [green]✓ Bot started[/]")
        if not telegram_bot.is_verified():
            console.print(f"  [yellow]Run /telegram activate to generate an activation code[/]")
        return

    if cmd == "stop":
        telegram_bot.stop()
        telegram_bot.set_enabled(False)
        console.print(f"  [yellow]✓ Bot stopped[/]")
        return

    if cmd == "reset":
        db.kv_set("telegram:owner_id", "")
        db.kv_set("telegram:owner_username", "")
        telegram_bot.clear_verification()
        console.print(f"  [yellow]✓ Owner reset. Re-verify needed.[/]")
        return

    console.print(f"  [dim]Unknown: /telegram {cmd}[/]")


def doctor():
    """Run diagnostics on all qwe-qwe components."""
    import time as _time

    console.print("\n  [bold yellow]⚡ qwe-qwe doctor[/]\n")
    passed = 0
    failed = 0
    warnings = 0

    def check(name, fn):
        nonlocal passed, failed, warnings
        try:
            result = fn()
            if result is True or (isinstance(result, str) and result.startswith("✓")):
                msg = result if isinstance(result, str) else "✓"
                console.print(f"  [green]✓[/] {name}: {msg}")
                passed += 1
            elif isinstance(result, str) and result.startswith("⚠"):
                console.print(f"  [yellow]⚠[/] {name}: {result}")
                warnings += 1
            else:
                console.print(f"  [red]✗[/] {name}: {result}")
                failed += 1
        except Exception as e:
            console.print(f"  [red]✗[/] {name}: {str(e)[:100]}")
            failed += 1

    # ── 1. Python & Dependencies ──
    console.print("  [dim]── Core ──[/]")
    import sys
    check("Python", lambda: f"✓ {sys.version.split()[0]}")

    def _check_deps():
        missing = []
        for mod in ["openai", "qdrant_client", "fastapi", "uvicorn", "rich", "requests"]:
            try:
                __import__(mod)
            except ImportError:
                missing.append(mod)
        if missing:
            return f"missing: {', '.join(missing)}"
        return True
    check("Dependencies", _check_deps)

    # ── 2. Database ──
    console.print("  [dim]── Storage ──[/]")
    def _check_db():
        conn = db._get_conn()
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        kv_count = conn.execute("SELECT COUNT(*) FROM kv").fetchone()[0]
        return f"✓ {len(tables)} tables, {msg_count} messages, {kv_count} settings"
    check("SQLite", _check_db)

    # ── 3. Qdrant ──
    def _check_qdrant():
        import memory as mem
        count = mem.count()
        return f"✓ {count} memories ({config.QDRANT_MODE} mode)"
    check("Qdrant", _check_qdrant)

    # ── 4. LLM Provider ──
    console.print("  [dim]── LLM ──[/]")
    def _check_provider():
        p = providers.get_active_name()
        model = providers.get_model()
        url = providers.get_url()
        return f"✓ {model} @ {p} ({url})"
    check("Provider", _check_provider)

    def _check_llm_connection():
        import requests as _req
        url = providers.get_url().rstrip("/")
        try:
            r = _req.get(f"{url}/models", timeout=5)
            if r.ok:
                models = r.json().get("data", [])
                return f"✓ {len(models)} models available"
            return f"HTTP {r.status_code}"
        except _req.ConnectionError:
            return "connection refused — is LM Studio running?"
        except Exception as e:
            return str(e)[:80]
    check("LLM API", _check_llm_connection)

    def _check_model_loaded():
        active = providers.get_active_name()
        if active not in ("lmstudio", "ollama"):
            return "✓ cloud provider (always available)"
        import requests as _req
        url = providers.get_url().rstrip("/").replace("/v1", "")
        model = providers.get_model()
        try:
            r = _req.get(f"{url}/api/v1/models", timeout=5)
            if r.ok:
                for m in r.json().get("models", []):
                    if m.get("key") == model and m.get("loaded_instances"):
                        return f"✓ {model} loaded in memory"
                return f"⚠ {model} downloaded but NOT loaded (will auto-load on first request)"
        except Exception:
            pass
        return f"⚠ could not check model status"
    check("Model loaded", _check_model_loaded)

    # ── 5. Embeddings ──
    def _check_embeddings():
        import requests as _req
        try:
            r = _req.get(f"{config.EMBED_BASE_URL}/models", timeout=5)
            if r.ok:
                models = [m["id"] for m in r.json().get("data", [])]
                if config.EMBED_MODEL in models:
                    return f"✓ {config.EMBED_MODEL}"
                return f"⚠ {config.EMBED_MODEL} not found (available: {', '.join(models[:3])})"
            return f"HTTP {r.status_code}"
        except Exception as e:
            return str(e)[:80]
    check("Embeddings", _check_embeddings)

    # ── 6. Inference test ──
    def _check_inference():
        t0 = _time.time()
        try:
            client = providers.get_client()
            providers.ensure_model_loaded()
            resp = client.chat.completions.create(
                model=providers.get_model(),
                messages=[{"role": "user", "content": "Say 'ok' and nothing else."}],
                max_tokens=10,
                temperature=0,
            )
            elapsed = _time.time() - t0
            reply = (resp.choices[0].message.content or "").strip()
            tokens = resp.usage.completion_tokens if resp.usage else "?"
            return f"✓ replied '{reply[:20]}' in {elapsed:.1f}s ({tokens} tokens)"
        except Exception as e:
            return str(e)[:100]
    check("Inference", _check_inference)

    # ── 7. Telegram ──
    console.print("  [dim]── Integrations ──[/]")
    def _check_telegram():
        import telegram_bot as tb
        s = tb.status()
        if not s["has_token"]:
            return "⚠ no token configured"
        if not s["running"]:
            return "⚠ not running (enable in settings)"
        if not s["verified"]:
            return f"⚠ running as @{s['username']} but not verified"
        return f"✓ @{s['username']} (owner: @{s['owner_username']})"
    check("Telegram", _check_telegram)

    # ── 8. Threads ──
    def _check_threads():
        all_t = threads.list_all()
        active = threads.get_active_id()
        return f"✓ {len(all_t)} threads, active: {active}"
    check("Threads", _check_threads)

    # ── 9. Skills ──
    def _check_skills():
        active = skills.get_active()
        all_s = skills.list_all()
        return f"✓ {len(active)}/{len(all_s)} active"
    check("Skills", _check_skills)

    # ── 10. Tools ──
    def _check_tools():
        import tools as t
        all_tools = t.get_all_tools()
        return f"✓ {len(all_tools)} tools registered"
    check("Tools", _check_tools)

    # ── 11. Disk ──
    console.print("  [dim]── System ──[/]")
    def _check_disk():
        import shutil
        total, used, free = shutil.disk_usage(".")
        free_gb = free / (1024**3)
        if free_gb < 1:
            return f"⚠ only {free_gb:.1f}GB free"
        return f"✓ {free_gb:.1f}GB free"
    check("Disk", _check_disk)

    # ── 12. Logs ──
    def _check_logs():
        from pathlib import Path
        log_path = Path("logs/qwe-qwe.log")
        if not log_path.exists():
            return "⚠ no log file"
        size_kb = log_path.stat().st_size / 1024
        return f"✓ {size_kb:.0f}KB"
    check("Logs", _check_logs)

    # ── Summary ──
    console.print()
    total = passed + failed + warnings
    if failed == 0 and warnings == 0:
        console.print(f"  [bold green]All {total} checks passed! ⚡[/]")
    elif failed == 0:
        console.print(f"  [bold yellow]{passed} passed, {warnings} warnings[/]")
    else:
        console.print(f"  [bold red]{passed} passed, {failed} failed, {warnings} warnings[/]")
    console.print()
    return failed == 0


def _run_update_cli():
    """Run update from CLI with Rich progress output."""
    import updater

    console.print("\n  [bold yellow]⚡ qwe-qwe update[/]\n")

    status_icons = {
        "checking": "🔍", "running": "⏳", "stashing": "📦",
        "ok": "[green]✓[/]", "error": "[red]✗[/]",
        "warning": "[yellow]⚠[/]", "skipped": "[dim]–[/]",
    }
    step_names = {
        "preflight": "Preflight", "fetch": "Check updates", "conflicts": "Skill conflicts",
        "backup": "Backup database", "pull": "Pull code", "deps": "Dependencies",
        "migrate": "Migrations", "done": "Complete",
    }

    def on_progress(step, status, detail=""):
        icon = status_icons.get(status, "·")
        name = step_names.get(step, step)
        if detail:
            console.print(f"  {icon} {name}: {detail}")
        else:
            console.print(f"  {icon} {name}")

    result = updater.perform_update(on_progress=on_progress)
    console.print()

    if result["success"]:
        if result["restart_needed"]:
            console.print(f"  [bold green]⚡ Updated: v{result['old_version']} → v{result['new_version']}[/]")
            console.print(f"  [dim]Restart qwe-qwe to use the new version.[/]\n")
        else:
            console.print(f"  [green]Already up to date (v{result['new_version']})[/]\n")
    else:
        console.print(f"  [bold red]Update failed:[/] {result.get('error', 'Unknown error')}\n")

    sys.exit(0 if result["success"] else 1)


def main_entry():
    """Unified entry point: `qwe-qwe` for CLI, `qwe-qwe --web` for web server."""
    import argparse
    parser = argparse.ArgumentParser(description="qwe-qwe — offline AI agent")
    parser.add_argument("--web", action="store_true", help="Start web server instead of CLI")
    parser.add_argument("--doctor", action="store_true", help="Run diagnostics")
    parser.add_argument("--update", action="store_true", help="Update to latest version")
    parser.add_argument("--host", default="0.0.0.0", help="Web server host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=7860, help="Web server port (default: 7860)")
    args = parser.parse_args()

    if args.update:
        _run_update_cli()
    elif args.doctor:
        doctor()
    elif args.web:
        import server
        server.start(host=args.host, port=args.port)
    else:
        main()


if __name__ == "__main__":
    main_entry()
