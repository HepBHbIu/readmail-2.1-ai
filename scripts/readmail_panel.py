#!/usr/bin/env python3
"""readmail_panel.py — визуальная TUI-панель Readmail (Textual).

Запуск без графики, прямо в терминале сервера:
    python3 scripts/readmail_panel.py
или через лаунчер:
    ./readmail

Сверху — живые цветные блоки состояния (сервер/почта/обработка/outbox/AI/воркеры),
снизу — чат: по `/` всплывает меню функций. Команды исполняет уже существующий
dispatch_shell_command() из readmailctl.py — бэкенд не дублируется и не меняется.

Безопасность наследуется от readmailctl: read-only, кроме /pause /resume; 1С и AI
не вызываются; опасные команды отключены; секреты не печатаются.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import readmailctl as ctl  # noqa: E402  (механика: статус + диспетчер команд)

from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.text import Text  # noqa: E402

from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import Horizontal, Vertical, VerticalScroll  # noqa: E402
from textual.widgets import Footer, Input, OptionList, RichLog, Static  # noqa: E402
from textual.widgets.option_list import Option  # noqa: E402


# ── Палитра команд для меню по «/» (label, шаблон, описание) ───────────────
COMMANDS: list[tuple[str, str, str]] = [
    ("/status", "/status", "снимок системы"),
    ("/refresh", "/refresh", "обновить блоки"),
    ("/search", "/search ", "единый поиск по письмам/кейсам/1С"),
    ("/trace", "/trace case ", "цепочка письмо→кейс→outbox→попытки"),
    ("/outbox", "/outbox", "сводка очереди в 1С"),
    ("/outbox preview", "/outbox preview ", "предпросмотр payload (read-only)"),
    ("/buckets", "/buckets", "где лежит каждое письмо (бухгалтерия)"),
    ("/folders", "/folders", "рабочие папки писем"),
    ("/pipeline", "/pipeline", "канонический конвейер (6 маршрутов)"),
    ("/processing", "/processing", "кейсы по стадиям"),
    ("/workers", "/workers", "состояние воркеров"),
    ("/pause", "/pause ", "пауза воркера (all|import|stage2|ai|outbox…)"),
    ("/resume", "/resume ", "снять паузу"),
    ("/mail", "/mail", "состояние почты"),
    ("/reconcile", "/reconcile", "снимок сверки IMAP↔БД"),
    ("/ai", "/ai", "AI-мозг и стоимость"),
    ("/settings", "/settings", "настройки (read-only, без секретов)"),
    ("/logs", "/logs", "последние события"),
    ("/doctor", "/doctor", "быстрый healthcheck"),
    ("/open", "/open", "URL веб-панели"),
    ("/help", "/help", "полный список команд"),
    ("/quit", "/quit", "выход"),
]


def _dot(ok: bool | None) -> Text:
    """Цветная лампочка состояния."""
    if ok is None:
        return Text("●", style="yellow")
    return Text("●", style="bright_green" if ok else "bright_red")


def _val(v: object, default: str = "—") -> str:
    if v is None or v == "":
        return default
    return str(v)


class StatBlock(Static):
    """Один блок-карточка. Рендерится Rich-панелью с цветной рамкой."""

    def __init__(self, key: str) -> None:
        super().__init__(id=f"blk-{key}")
        self.key = key

    def show(self, title: str, rows: list[tuple[str, object, str]], border: str) -> None:
        t = Table.grid(padding=(0, 1), expand=True)
        t.add_column(justify="left", ratio=1, style="grey70")
        t.add_column(justify="right", style="bold")
        for label, value, style in rows:
            t.add_row(label, Text(_val(value), style=style or "white"))
        self.update(Panel(t, title=f"[b]{title}[/b]", border_style=border,
                          title_align="left", padding=(0, 1)))


class CommandMenu(OptionList):
    """Выпадающее меню функций по «/». Фильтруется по вводу."""

    def populate(self, prefix: str) -> None:
        self.clear_options()
        p = prefix.lower()
        for label, template, desc in COMMANDS:
            if label.lower().startswith(p) or p in ("", "/"):
                opt = Text.assemble((f"{label:<18}", "bold cyan"), (desc, "grey70"))
                self.add_option(Option(opt, id=template))


class ReadmailPanel(App):
    TITLE = "READMAIL"
    CSS = """
    Screen { background: $surface; layers: base menu; }
    #header { height: 1; background: $panel; color: $text; padding: 0 1; }
    #grid { height: auto; }
    .row { height: 9; }
    StatBlock { width: 1fr; height: 100%; }
    #chat { height: 1fr; border: round $primary 40%; background: $surface;
            padding: 0 1; margin: 0 1; }
    #chat-title { height: 1; color: $text-muted; padding: 0 1; }
    #prompt { dock: bottom; height: 3; border: round $accent; margin: 0 1 1 1; }
    CommandMenu { layer: menu; dock: bottom; offset: 1 -4; width: 70%;
                  height: auto; max-height: 12; border: round $accent;
                  background: $panel; display: none; }
    CommandMenu.visible { display: block; }
    """
    BINDINGS = [
        Binding("ctrl+c", "quit", "Выход", show=False),
        Binding("ctrl+r", "refresh_now", "Обновить"),
        Binding("ctrl+l", "focus_prompt", "Ввод"),
        Binding("escape", "hide_menu", "Скрыть меню", show=False),
        Binding("ctrl+q", "quit", "Выход"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._status: dict = {}

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with Vertical(id="grid"):
            with Horizontal(classes="row"):
                yield StatBlock("server")
                yield StatBlock("mail")
                yield StatBlock("processing")
            with Horizontal(classes="row"):
                yield StatBlock("outbox")
                yield StatBlock("ai")
                yield StatBlock("workers")
        yield Static("чат · введите / для меню функций", id="chat-title")
        with VerticalScroll(id="chat"):
            yield RichLog(id="log", wrap=True, markup=True, highlight=False)
        yield CommandMenu(id="menu")
        yield Input(placeholder="›  введите / для команд или сообщение ассистенту…",
                    id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        ctl._bootstrap_db()
        log = self.query_one("#log", RichLog)
        log.write("[b cyan]READMAIL[/b cyan] — панель управления. "
                  "Блоки сверху обновляются сами. Внизу — чат: [b]/[/b] открывает меню функций.")
        self.refresh_status()
        self.set_interval(6.0, self.refresh_status)
        self.query_one("#prompt", Input).focus()

    # ── обновление блоков ─────────────────────────────────────────────
    def action_refresh_now(self) -> None:
        self.refresh_status()

    def refresh_status(self) -> None:
        self.run_worker(self._load_status, thread=True, exclusive=True)

    def _load_status(self) -> None:
        try:
            st = ctl.collect_terminal_status()
        except Exception as exc:  # noqa: BLE001
            st = {"ok": False, "error": str(exc)}
        self.call_from_thread(self._render_status, st)

    def _render_status(self, st: dict) -> None:
        self._status = st
        srv = st.get("server") or {}
        mail = st.get("mail") or {}
        proc = st.get("processing") or {}
        ob = st.get("outbox") or {}
        ai = st.get("ai") or {}
        wk = (st.get("workers") or {})
        workers = wk.get("workers") or {}

        srv_up = str(srv.get("status", "")).lower() in ("up", "running", "ok", "online")
        missing = mail.get("missing_local")
        mail_ok = (missing == 0) if isinstance(missing, int) else None
        delivery_on = bool(ob.get("delivery_enabled"))

        # шапка с лампочками
        head = Text()
        head.append("  READMAIL ", style="bold white on dark_blue")
        head.append(" МИНИ-АДМИН ", style="bold black on cyan")
        head.append(" read-only ", style="black on grey70")
        head.append("  ")
        for lbl, ok in (("сервер", srv_up), ("почта", mail_ok),
                        ("1С", delivery_on if delivery_on else False)):
            head.append_text(_dot(ok)); head.append(f" {lbl}   ", style="grey70")
        if wk.get("global_paused"):
            head.append("⏸ ПАУЗА  ", style="bold yellow")
        head.append(_val(st.get("generated_at", "")), style="grey50")
        self.query_one("#header", Static).update(head)

        self.query_one("#blk-server", StatBlock).show("СЕРВЕР", [
            ("статус", str(srv.get("status", "?")).upper(),
             "bright_green" if srv_up else "bright_red"),
            ("адрес", f"{srv.get('host', '?')}:{srv.get('port', '?')}", "cyan"),
            ("LAN", "вкл" if srv.get("allow_lan") else "выкл", "white"),
            ("пауза", "ВКЛ" if wk.get("global_paused") else "нет",
             "yellow" if wk.get("global_paused") else "grey70"),
        ], "green" if srv_up else "red")

        self.query_one("#blk-mail", StatBlock).show("ПОЧТА", [
            ("на сервере", mail.get("server_total"), "white"),
            ("в базе", mail.get("local_raw_total"), "white"),
            ("дыра", missing, "bright_red" if isinstance(missing, int) and missing else "bright_green"),
            ("карантин", mail.get("quarantine", 0), "yellow" if mail.get("quarantine") else "grey70"),
        ], "yellow" if mail_ok is False else "cyan")

        self.query_one("#blk-processing", StatBlock).show("ОБРАБОТКА", [
            ("кейсов", proc.get("cases_total"), "white"),
            ("без кейса", proc.get("raw_without_case"),
             "yellow" if proc.get("raw_without_case") else "grey70"),
            ("готово к 1С", proc.get("ready_to_1c", 0), "bright_green"),
            ("в сверке", proc.get("needs_review", 0),
             "magenta" if proc.get("needs_review") else "grey70"),
        ], "magenta")

        by = ob.get("by_status") or {}
        self.query_one("#blk-outbox", StatBlock).show("OUTBOX / 1С", [
            ("в очереди", by.get("new", 0), "cyan"),
            ("ошибки", by.get("error", 0), "bright_red" if by.get("error") else "grey70"),
            ("отправлено", by.get("sent", 0), "bright_green"),
            ("доставка", "ВКЛ" if delivery_on else "OFF",
             "bright_green" if delivery_on else "yellow"),
        ], "blue")

        self.query_one("#blk-ai", StatBlock).show("AI", [
            ("включён", "да" if ai.get("enabled") else "нет",
             "bright_green" if ai.get("enabled") else "grey70"),
            ("паттерны", "0 токенов", "bright_green"),
            ("вызовов", ai.get("calls_today", 0), "white"),
            ("₽ сегодня", ai.get("cost_today", 0), "yellow"),
        ], "bright_blue")

        wrows: list[tuple[str, object, str]] = []
        for name, v in list(workers.items())[:4]:
            state = v.get("state") if isinstance(v, dict) else v
            run = str(state).lower() in ("running", "active", "on")
            wrows.append((name, "▶ " + str(state) if run else "■ " + str(state),
                          "bright_green" if run else "grey70"))
        if not wrows:
            wrows = [("воркеры", "нет данных", "grey70")]
        self.query_one("#blk-workers", StatBlock).show("ВОРКЕРЫ", wrows, "cyan")

        # предупреждения в чат-заголовок
        warns = st.get("warnings") or []
        title = self.query_one("#chat-title", Static)
        if warns:
            title.update(Text("⚠ " + warns[0], style="yellow"))
        else:
            title.update(Text("чат · введите / для меню функций", style="grey50"))

    # ── чат и меню команд ─────────────────────────────────────────────
    def on_input_changed(self, event: Input.Changed) -> None:
        menu = self.query_one("#menu", CommandMenu)
        val = event.value
        if val.startswith("/") and " " not in val:
            menu.populate(val)
            menu.add_class("visible")
        else:
            menu.remove_class("visible")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        template = event.option.id or ""
        inp = self.query_one("#prompt", Input)
        inp.value = template
        inp.cursor_position = len(template)
        self.query_one("#menu", CommandMenu).remove_class("visible")
        inp.focus()
        if not template.endswith(" "):  # команда без аргументов — сразу выполнить
            self._run_line(template)
            inp.value = ""

    def action_hide_menu(self) -> None:
        self.query_one("#menu", CommandMenu).remove_class("visible")

    def action_focus_prompt(self) -> None:
        self.query_one("#prompt", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        self.query_one("#menu", CommandMenu).remove_class("visible")
        event.input.value = ""
        if not line:
            return
        self._run_line(line)

    def _run_line(self, line: str) -> None:
        log = self.query_one("#log", RichLog)
        log.write(Text.assemble(("› ", "bold cyan"), (line, "bold white")))
        if not line.startswith("/"):
            log.write(Text("  ассистент: я понимаю команды через «/». "
                           "Нажмите / — покажу меню функций.", style="grey62"))
            return
        self.run_worker(lambda: self._exec(line), thread=True, exclusive=False)

    def _exec(self, line: str) -> None:
        try:
            res = ctl.dispatch_shell_command(line)
            text = res.text or "(пусто)"
            should_exit = res.should_exit
        except Exception as exc:  # noqa: BLE001
            text, should_exit = f"❌ ошибка: {exc}", False
        self.call_from_thread(self._show_result, text, should_exit)

    def _show_result(self, text: str, should_exit: bool) -> None:
        log = self.query_one("#log", RichLog)
        for ln in text.splitlines() or [""]:
            log.write(Text(ln, style="grey85"))
        log.write("")
        if should_exit:
            self.exit()
        self.refresh_status()


def main() -> int:
    ReadmailPanel().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
