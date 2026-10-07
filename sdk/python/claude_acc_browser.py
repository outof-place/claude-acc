"""Driver toolsetu przeglądarki z Anthropic SDK (browser_toolset_20260801) na bramce claude-acc.

SDK prowadzi pętlę, polityki i zgody, a ten driver wykonuje każde wywołanie w Twoim Chrome albo
Brave przez demon claude-acc: ukryte karty bez fokusu, Twoje loginy, jedno "Allow" na start
przeglądarki, ta sama bramka domen i ten sam dziennik co MCP `browser` w Claude Code.

    from anthropic import Anthropic
    from claude_acc_browser import ClaudeAccBrowser

    with ClaudeAccBrowser() as browser:
        runner = Anthropic().beta.messages.tool_runner(
            model="claude-opus-5-5", max_tokens=4096, tools=[browser],
            messages=[{"role": "user", "content": "Open example.com and tell me the heading."}],
        )
        for message in runner:
            print(message)

Wymaga zainstalowanego claude-acc (`claude-acc browser install`) i `anthropic>=1.12`. Tryb
`full` bramki (`claude-acc browser mode full`) włącza javascript_exec i file_upload bez pytania;
w trybie guarded podajesz własne `confirm` i `file_policy`, jak w dokumentacji SDK.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import anyio.to_thread
from anthropic.tools import ToolError
from anthropic.tools.browser import (
    BetaAbstractBrowserToolset20260801,
    BetaAsyncAbstractBrowserToolset20260801,
    BetaBrowserNavigateResult,
    BetaBrowserState,
    BetaDialogDismissed,
    BetaNavigationRefused,
    BetaScreenshotResult,
)

STATE = os.environ.get("CLAUDE_ACC_STATE") or os.path.expanduser(
    "~/.local/share/claude-acc"
)
# demon zbiera konsolę i sieć każdej karty, więc te dwa opcjonalne członki są zawsze włączone
ALWAYS_ON = ("read_console", "read_network")
GATED = ("file_upload", "javascript_exec")

__all__ = ["AsyncClaudeAccBrowser", "ClaudeAccBrowser"]


def _gateway() -> Any:
    """browser.py z instalacji claude-acc: klient demona i jego błędy (sam stdlib)."""
    if STATE not in sys.path:
        sys.path.insert(0, STATE)
    import browser  # type: ignore[import-not-found]

    return browser


class _Core:
    """Rozmowa z demonem i przekład jego odpowiedzi na typy SDK; wspólne dla wersji sync i async."""

    def __init__(self, browser: str | None, owner: str) -> None:
        self.gw = _gateway()
        self.client = self.gw.HubClient(owner, "sdk:python", True, browser)
        self.state: dict[str, Any] | None = None

    def mode(self) -> str:
        try:
            return str(self.gw.load_config()["mode"])
        except self.gw.BrowserError:
            return "guarded"

    def execute(self, name: str, input: Any) -> Any:
        args = input.to_dict() if hasattr(input, "to_dict") else dict(input or {})
        try:
            reply = self.client.call(name, args, timeout=600)
        except self.gw.StateError as exc:
            self.state = exc.state
            raise ToolError(str(exc)) from None
        except self.gw.BrowserError as exc:
            raise ToolError(str(exc)) from None
        self.state = reply.get("state")
        r = reply.get("result") or {}
        kind = r.get("kind")
        if kind == "navigate":
            return BetaBrowserNavigateResult(
                url=r["url"], status=r.get("status"), title=r.get("title")
            )
        if kind == "image":
            return BetaScreenshotResult(
                data=r["data"], media_type=r.get("media_type") or "image/jpeg"
            )
        if kind == "text":
            return r.get("text") or ""
        if kind == "tab":
            return r["tab"]
        if kind == "tabs":
            return r["tabs"]
        return (
            None  # czysta akcja: SDK dopisuje własne potwierdzenie (Clicked., Typed.)
        )

    def browser_state(self) -> BetaBrowserState:
        state, self.state = self.state, None
        if state is None:  # wywołanie odmówione przez SDK, zanim doszło do demona
            state = self.client.call("state", {}, timeout=60)["state"]
        changes: list[Any] = []
        for change in state.get("state_changes") or []:
            if change["type"] == "dialog_dismissed":
                changes.append(
                    BetaDialogDismissed(
                        kind=change.get("kind") or "dialog",
                        message=change.get("message") or "",
                    )
                )
            elif change["type"] == "navigation_refused":
                changes.append(BetaNavigationRefused())
            else:
                changes.append(change)
        return BetaBrowserState(tabs=state.get("tabs") or [], state_changes=changes)

    def close(self) -> None:
        try:
            self.client.call("close_all", {}, timeout=60)
        except self.gw.BrowserError:
            pass
        self.client.close()


def _options(core: _Core, full: bool | None, options: dict[str, Any]) -> dict[str, Any]:
    full = core.mode() == "full" if full is None else full
    configs = {name: {"enabled": True} for name in ALWAYS_ON}
    if full:
        configs.update({name: {"enabled": True} for name in GATED})
        if options.get("confirm") is None:
            options["confirm"] = lambda context: (
                True
            )  # tryb full: agent może wszystko, bez pytania
    configs.update(options.get("configs") or {})
    options["configs"] = configs
    return options


class ClaudeAccBrowser(BetaAbstractBrowserToolset20260801):
    """Toolset przeglądarki na Twoim Chrome albo Brave przez claude-acc.

    Args:
        browser: "chrome" albo "brave"; domyślnie wybór claude-acc (`claude-acc browser use`).
        full: nadpisuje tryb bramki dla javascript_exec i file_upload (domyślnie z konfiguracji).
        **options: configs, confirm, url_policy, file_policy, tool_configs jak w SDK.
    """

    def __init__(
        self, *, browser: str | None = None, full: bool | None = None, **options: Any
    ) -> None:
        core = _Core(browser, f"sdk:{os.getpid()}:{id(self):x}")
        super().__init__(**_options(core, full, options))
        self._core = core

    def execute(self, context: Any, name: Any, input: Any) -> Any:
        return self._core.execute(name, input)

    def _browser_state(self, context: Any) -> BetaBrowserState:
        return self._core.browser_state()

    def close(self) -> None:
        super().close()  # najpierw SDK: żadne wywołanie nie używa już przeglądarki
        self._core.close()


class AsyncClaudeAccBrowser(BetaAsyncAbstractBrowserToolset20260801):
    """To samo dla `AsyncAnthropic`: rozmowa z demonem idzie w wątku, pętla zdarzeń się nie blokuje."""

    def __init__(
        self, *, browser: str | None = None, full: bool | None = None, **options: Any
    ) -> None:
        core = _Core(browser, f"sdk:{os.getpid()}:{id(self):x}")
        super().__init__(**_options(core, full, options))
        self._core = core

    async def execute(self, context: Any, name: Any, input: Any) -> Any:
        return await anyio.to_thread.run_sync(self._core.execute, name, input)

    async def _browser_state(self, context: Any) -> BetaBrowserState:
        return await anyio.to_thread.run_sync(self._core.browser_state)

    async def close(self) -> None:
        await super().close()
        await anyio.to_thread.run_sync(self._core.close)
