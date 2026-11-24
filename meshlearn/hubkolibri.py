"""Hub pages for picking Kolibri topics for the memory card (see kolibri.py)."""

from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlencode

from . import catalog as cat
from . import kolibri
from .web import Request, Response, esc, redirect

KIND_NAMES = {"mp4": "Video", "webm": "Video", "mp3": "Audio", "pdf": "PDF", "epub": "E-book", "zip": "Activity"}


class HubKolibri:
    """Mixed into Hub: uses its catalog, selection and _page."""

    def add_kolibri_routes(self, app) -> None:
        app.add_route("GET", "/library/kolibri", self.kolibri_page)
        app.add_route("POST", "/library/kolibri/open", self.kolibri_open)
        app.add_route("POST", "/library/kolibri/add", self.kolibri_add)

    def _kolibri_entry(self, channel_id: str) -> Optional[dict]:
        return self.catalog.find(f"kolibri:{channel_id}", "")

    @staticmethod
    def _kolibri_link(channel_id: str, node: str = "", msg: str = "") -> str:
        return "/library/kolibri?" + urlencode({k: v for k, v in {"id": channel_id, "topic": node, "msg": msg}.items() if v})

    def kolibri_page(self, req: Request) -> Response:
        channel_id = req.query.get("id", "")
        entry = self._kolibri_entry(channel_id)
        if not entry:
            return redirect("/library?" + urlencode({"msg": "That Kolibri channel is not in the list. Update the list and try again."}))
        channel = kolibri.Channel(self.kolibri_dir, channel_id)
        msg = req.query.get("msg", "")
        head = (f"<p class='warn' role='status'>{esc(msg)}</p>" if msg else "") + "<p><a href='/library'>← Card library</a></p>"
        get_list = ("<form method='post' action='/library/kolibri/open'>"
                    f"<input type='hidden' name='id' value='{esc(channel_id)}'><button{{cls}}>{{text}}</button></form>")
        if not channel.ready:
            body = (head + f"<div class='card'><p>{esc(entry['summary'])}</p><p>To choose topics, MeshLearn first downloads this "
                    "channel's list of lessons, usually a few MB. It needs internet once.</p>"
                    + get_list.format(cls="", text="Get the list of topics") + "</div>")
            return Response(self._page(entry["title"], body, current="library", sub="Kolibri channel"))
        node = channel.node(req.query.get("topic", ""))
        if not node:
            return redirect(self._kolibri_link(channel_id))
        lessons = channel.lessons(node)
        size = sum({x["file"]: x["file_size"] for x in lessons}.values())
        selection = self.selection()
        used, budget = cat.total_bytes(selection), self.library_budget()
        here = self._kolibri_link(channel_id, node["id"])
        trail = channel.trail(node)
        crumbs = " › ".join([f"<a href='{esc(self._kolibri_link(channel_id, t['id']))}'>"
                             f"{esc(entry['title'] if t['level'] == 0 else t['title'])}</a>" for t in trail]
                            + [esc(entry["title"] if node["level"] == 0 else node["title"])])
        rows = ""
        for c in channel.children(node, lessons):
            if c["kind"] == "topic":
                name = f"<a href='{esc(self._kolibri_link(channel_id, c['id']))}'><strong>{esc(c['title'])}</strong></a>"
                about = f"{c['lessons']:,} lesson{'' if c['lessons'] == 1 else 's'}"
            else:
                name, about = f"<strong>{esc(c['title'])}</strong>", KIND_NAMES.get(c["extension"], c["extension"])
            rows += (f"<tr><td>{name}<div class='muted'>{esc(about)} · {self._gb(c['size'])}</div></td>"
                     f"<td>{self._kolibri_button(channel_id, c, c['lessons'], selection, here)}</td></tr>")
        files = len({x["file"] for x in lessons})
        left_out = channel.left_out(node, lessons)
        body = (
            head + f"<p>{crumbs}</p>"
            f"<div class='card'><p><strong>{files:,} lesson{'' if files == 1 else 's'}</strong> · {self._gb(size)} here. "
            f"Chosen for the card so far: <strong>{self._gb(used)}</strong> of {self._gb(budget)}"
            + (f" · <span class='bad'>{self._gb(used - budget)} too much</span>" if used > budget else "") + "</p>"
            + (self._kolibri_button(channel_id, node, files, selection, here, all_of_it=True) if files else
               "<p>Nothing here can go on the card.</p>")
            + "</div>"
            + (f"<table><tr><th>Topic or lesson</th><th>Add to card</th></tr>{rows}</table>" if rows else "")
            + "<p class='muted'>Videos, sound, PDFs, e-books and activities with an open licence, which students open in the MeshLearn "
            "library without the Kolibri app. A lesson listed under several topics is copied once, so topics can add up to "
            "less than the sum of their sizes."
            + (f" Left out here: {left_out:,} exercise{'' if left_out == 1 else 's'} and items with other licences." if left_out else "")
            + "</p>" + get_list.format(cls=" class='secondary'", text="Get the newest list of topics")
        )
        return Response(self._page(entry["title"], body, current="library", sub="Kolibri channel"))

    def _kolibri_button(self, channel_id: str, node: dict, files: int, selection: List[dict], here: str,
                        all_of_it: bool = False) -> str:
        key = f"kolibri:{channel_id}:{node['id']}|"
        me = {"key": key, "kolibri": {"channel": channel_id, "lft": node["lft"], "rght": node["rght"]}}
        for item in selection:
            if item["key"] == key:
                return ("<form method='post' action='/library/remove' style='display:inline'>"
                        f"<input type='hidden' name='key' value='{esc(key)}'><input type='hidden' name='back' value='{esc(here)}'>"
                        f"<button aria-pressed='true'>✓ Added{' · remove' if all_of_it else ''}</button></form>")
            if kolibri.part_of(me, item):
                return f"<span class='good'>✓ Included in “{esc(item['title'])}”</span>"
        if files > kolibri.MAX_FILES:
            return "<span class='muted'>Too many lessons at once: open it and choose topics</span>"
        some = sum(1 for item in selection if kolibri.part_of(item, me))
        note = f" <span class='muted'>{some} topic{'' if some == 1 else 's'} inside already added</span>" if some else ""
        text = "Add all of it" if all_of_it else "Add"
        return ("<form method='post' action='/library/kolibri/add' style='display:inline'>"
                f"<input type='hidden' name='id' value='{esc(channel_id)}'><input type='hidden' name='topic' value='{esc(node['id'])}'>"
                f"<input type='hidden' name='back' value='{esc(here)}'><button class='secondary'>{text}</button></form>{note}")

    def kolibri_open(self, req: Request) -> Response:
        channel_id = req.form.get("id", "")
        if not self._kolibri_entry(channel_id):
            return redirect("/library")
        if not self._local(req):
            return redirect(self._kolibri_link(channel_id, msg="Get the list from the hub laptop itself."))
        try:
            kolibri.Channel(self.kolibri_dir, channel_id).fetch()
        except kolibri.KolibriError as exc:
            return redirect(self._kolibri_link(channel_id, msg=f"Sorry, {exc}."))
        except Exception as exc:
            return redirect(self._kolibri_link(channel_id, msg=f"Could not get the list ({exc}). Is this laptop online?"))
        return redirect(self._kolibri_link(channel_id))

    def kolibri_add(self, req: Request) -> Response:
        channel_id = req.form.get("id", "")
        entry = self._kolibri_entry(channel_id)
        if not entry:
            return redirect("/library")
        channel = kolibri.Channel(self.kolibri_dir, channel_id)
        node = channel.node(req.form.get("topic", "")) if channel.ready else None
        if not node:
            return redirect(self._kolibri_link(channel_id))
        try:
            item = channel.selection_item(node, entry)
        except kolibri.KolibriError as exc:
            return redirect(self._kolibri_link(channel_id, node["parent_id"] or "", msg=f"Not added: {exc}."))
        for other in self.selection():
            if kolibri.part_of(item, other):
                return self._back(req)
            if kolibri.part_of(other, item):  # the bigger topic replaces the ones inside it
                self.db.execute("DELETE FROM library_selection WHERE key = ?", (other["key"],))
        self.select_item(item)
        return self._back(req)
