"""Modelo de selección masiva para la TUI. Puro, sin Textual (testable).

Regla: un peer blindado (safety.hard_exclusions no vacío) NO puede
seleccionarse jamás — la TUI solo refleja esta decisión, no la toma.
"""

from __future__ import annotations

from tg_sentry.models import DialogInfo
from tg_sentry.safety import hard_exclusions, parse_whitelist


class SelectionModel:
    def __init__(
        self,
        *,
        me_id: int | None = None,
        whitelist: list[str] | None = None,
        contact_ids: set[int] | None = None,
        protect_contacts: bool = True,
    ) -> None:
        self._me_id = me_id
        self._whitelist_ids, self._whitelist_usernames = parse_whitelist(whitelist or [])
        self._contact_ids = contact_ids
        self._protect_contacts = protect_contacts
        self.selected: set[int] = set()

    def lock_reason(self, dialog: DialogInfo) -> str | None:
        """Razón por la que el peer está blindado; None si es seleccionable."""
        reasons = hard_exclusions(
            dialog,
            me_id=self._me_id,
            whitelist_ids=self._whitelist_ids,
            whitelist_usernames=self._whitelist_usernames,
            contact_ids=self._contact_ids,
            protect_contacts=self._protect_contacts,
        )
        return "; ".join(reasons) if reasons else None

    def toggle(self, dialog: DialogInfo) -> bool:
        """Alterna la selección; devuelve False si está blindado."""
        if self.lock_reason(dialog) is not None:
            return False
        if dialog.id in self.selected:
            self.selected.discard(dialog.id)
        else:
            self.selected.add(dialog.id)
        return True

    def select_all(self, dialogs: list[DialogInfo]) -> int:
        """Selecciona todos los seleccionables de la lista; devuelve cuántos
        se AÑADIERON en esta llamada (delta de la vista, no total global)."""
        añadidos = 0
        for dialog in dialogs:
            if self.lock_reason(dialog) is None and dialog.id not in self.selected:
                self.selected.add(dialog.id)
                añadidos += 1
        return añadidos

    def invert(self, dialogs: list[DialogInfo]) -> int:
        """Invierte la selección de la lista; devuelve el delta de esta vista."""
        delta = 0
        for dialog in dialogs:
            if self.lock_reason(dialog) is not None:
                continue
            if dialog.id in self.selected:
                self.selected.discard(dialog.id)
                delta -= 1
            else:
                self.selected.add(dialog.id)
                delta += 1
        return delta

    def clear(self) -> None:
        self.selected.clear()

    def selected_dialogs(self, dialogs: list[DialogInfo]) -> list[DialogInfo]:
        """Los diálogos seleccionados, en el orden de la lista."""
        return [dialog for dialog in dialogs if dialog.id in self.selected]

    def __len__(self) -> int:
        return len(self.selected)
