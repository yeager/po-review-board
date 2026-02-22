"""PO Review Board — Review .po translations with diff view, comments and approval."""
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk, Gio, GLib, Pango

import gettext
import locale
import os
import sys
import json
import datetime
import re
import difflib
from po_review_board.accessibility import AccessibilityManager

LOCALE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "po")
if not os.path.isdir(LOCALE_DIR):
    LOCALE_DIR = "/usr/share/locale"
locale.bindtextdomain("po-review-board", LOCALE_DIR)
gettext.bindtextdomain("po-review-board", LOCALE_DIR)
gettext.textdomain("po-review-board")
_ = gettext.gettext

APP_ID = "se.danielnylander.po-review-board"
SETTINGS_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "po-review-board"
)
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")
REVIEWS_DIR = os.path.join(SETTINGS_DIR, "reviews")


def _load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    return {"welcome_shown": False}


def _save_settings(s):
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f, indent=2)


# ── PO parser ────────────────────────────────────────────────

class POEntry:
    def __init__(self):
        self.msgid = ""
        self.msgstr = ""
        self.msgctxt = ""
        self.comments = []
        self.flags = []
        self.line_no = 0
        self.review_status = ""  # "", "approved", "rejected", "needs-work"
        self.review_comment = ""


def parse_po(text):
    """Parse a .po file into a list of POEntry objects."""
    entries = []
    current = POEntry()
    last_field = None
    line_no = 0

    for raw_line in text.splitlines():
        line_no += 1
        line = raw_line.strip()

        if not line:
            if current.msgid or current.msgstr:
                entries.append(current)
            current = POEntry()
            current.line_no = line_no
            last_field = None
            continue

        if line.startswith("#,"):
            current.flags = [f.strip() for f in line[2:].split(",")]
        elif line.startswith("#"):
            current.comments.append(line)
        elif line.startswith("msgctxt "):
            current.msgctxt = _unquote(line[8:])
            last_field = "msgctxt"
        elif line.startswith("msgid "):
            current.msgid = _unquote(line[6:])
            current.line_no = line_no
            last_field = "msgid"
        elif line.startswith("msgstr "):
            current.msgstr = _unquote(line[7:])
            last_field = "msgstr"
        elif line.startswith('"') and last_field:
            val = _unquote(line)
            if last_field == "msgid":
                current.msgid += val
            elif last_field == "msgstr":
                current.msgstr += val
            elif last_field == "msgctxt":
                current.msgctxt += val

    if current.msgid or current.msgstr:
        entries.append(current)

    # Skip header entry
    return [e for e in entries if e.msgid]


def _unquote(s):
    s = s.strip()
    if s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    return s.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')


# ── Review storage ───────────────────────────────────────────

def _review_path(po_path):
    name = os.path.basename(po_path).replace(".po", "").replace(".pot", "")
    os.makedirs(REVIEWS_DIR, exist_ok=True)
    return os.path.join(REVIEWS_DIR, f"{name}.json")


def _load_reviews(po_path):
    p = _review_path(po_path)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {}


def _save_reviews(po_path, reviews):
    with open(_review_path(po_path), "w") as f:
        json.dump(reviews, f, indent=2)


# ── Main Window ──────────────────────────────────────────────

class ReviewWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=_("PO Review Board"), default_width=1100, default_height=750)
        self.settings = _load_settings()
        self._po_path = None
        self._po_path_old = None
        self._entries = []
        self._reviews = {}
        self._filter_mode = "all"  # all, untranslated, fuzzy, approved, rejected

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Header
        headerbar = Adw.HeaderBar()
        title_widget = Adw.WindowTitle(title=_("PO Review Board"), subtitle="")
        headerbar.set_title_widget(title_widget)
        self._title_widget = title_widget

        open_btn = Gtk.Button(icon_name="document-open-symbolic", tooltip_text=_("Open .po file"))
        open_btn.connect("clicked", self._on_open)
        headerbar.pack_start(open_btn)

        diff_btn = Gtk.Button(icon_name="view-dual-symbolic", tooltip_text=_("Compare with another .po file"))
        diff_btn.connect("clicked", self._on_open_diff)
        headerbar.pack_start(diff_btn)

        # Filter dropdown
        filter_model = Gtk.StringList.new([
            _("All entries"), _("Untranslated"), _("Fuzzy"),
            _("Approved"), _("Rejected"), _("Needs work")
        ])
        self._filter_drop = Gtk.DropDown(model=filter_model)
        self._filter_drop.connect("notify::selected", self._on_filter_changed)
        headerbar.pack_start(self._filter_drop)

        # Stats label
        self._stats_label = Gtk.Label(label="")
        self._stats_label.add_css_class("dim-label")
        headerbar.pack_end(self._stats_label)

        # Menu
        menu = Gio.Menu()
        menu.append(_("Export Review Report"), "app.export-report")
        menu.append(_("Copy Debug Info"), "app.copy-debug")
        menu.append(_("Keyboard Shortcuts"), "app.shortcuts")
        menu.append(_("About PO Review Board"), "app.about")
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        headerbar.pack_end(menu_btn)

        main_box.append(headerbar)

        # Content: paned
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_vexpand(True)

        # Left: entry list
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        left_box.set_size_request(350, -1)

        # Search
        self._search_entry = Gtk.SearchEntry(placeholder_text=_("Search entries..."))
        self._search_entry.set_margin_start(8)
        self._search_entry.set_margin_end(8)
        self._search_entry.set_margin_top(8)
        self._search_entry.connect("search-changed", self._on_search_changed)
        left_box.append(self._search_entry)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        self._entry_list = Gtk.ListBox()
        self._entry_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._entry_list.connect("row-selected", self._on_entry_selected)
        scroll.set_child(self._entry_list)
        left_box.append(scroll)

        paned.set_start_child(left_box)

        # Right: detail view
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right_box.set_margin_start(12)
        right_box.set_margin_end(12)
        right_box.set_margin_top(8)
        right_box.set_margin_bottom(8)

        # Status page when no file loaded
        self._empty_status = Adw.StatusPage()
        self._empty_status.set_icon_name("document-edit-symbolic")
        self._empty_status.set_title(_("No file loaded"))
        self._empty_status.set_description(_("Open a .po or .pot file to start reviewing translations."))
        self._empty_status.set_vexpand(True)

        # Detail panel (hidden initially)
        self._detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._detail_box.set_visible(False)
        self._detail_box.set_vexpand(True)

        # Context
        self._ctx_label = Gtk.Label(label="", xalign=0, wrap=True)
        self._ctx_label.add_css_class("dim-label")
        self._detail_box.append(self._ctx_label)

        # Source (msgid)
        src_label = Gtk.Label(label=_("Source (msgid)"), xalign=0)
        src_label.add_css_class("heading")
        self._detail_box.append(src_label)

        self._msgid_view = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self._msgid_view.set_monospace(True)
        self._msgid_view.set_top_margin(8)
        self._msgid_view.set_bottom_margin(8)
        self._msgid_view.set_left_margin(8)
        msgid_scroll = Gtk.ScrolledWindow(min_content_height=80, max_content_height=200)
        msgid_scroll.set_child(self._msgid_view)
        self._detail_box.append(msgid_scroll)

        # Translation (msgstr)
        tr_label = Gtk.Label(label=_("Translation (msgstr)"), xalign=0)
        tr_label.add_css_class("heading")
        self._detail_box.append(tr_label)

        self._msgstr_view = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self._msgstr_view.set_monospace(True)
        self._msgstr_view.set_top_margin(8)
        self._msgstr_view.set_bottom_margin(8)
        self._msgstr_view.set_left_margin(8)
        msgstr_scroll = Gtk.ScrolledWindow(min_content_height=80, max_content_height=200)
        msgstr_scroll.set_child(self._msgstr_view)
        self._detail_box.append(msgstr_scroll)

        # Diff view (hidden by default)
        self._diff_label = Gtk.Label(label=_("Diff"), xalign=0)
        self._diff_label.add_css_class("heading")
        self._diff_label.set_visible(False)
        self._detail_box.append(self._diff_label)

        self._diff_view = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self._diff_view.set_monospace(True)
        self._diff_view.set_top_margin(8)
        self._diff_view.set_bottom_margin(8)
        self._diff_view.set_left_margin(8)
        self._diff_scroll = Gtk.ScrolledWindow(min_content_height=60, max_content_height=150)
        self._diff_scroll.set_child(self._diff_view)
        self._diff_scroll.set_visible(False)
        self._detail_box.append(self._diff_scroll)

        # PO comments
        self._po_comments = Gtk.Label(label="", xalign=0, wrap=True)
        self._po_comments.add_css_class("dim-label")
        self._po_comments.add_css_class("caption")
        self._detail_box.append(self._po_comments)

        # Review actions
        action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_box.set_margin_top(8)

        approve_btn = Gtk.Button(label=_("Approve"))
        approve_btn.add_css_class("suggested-action")
        approve_btn.connect("clicked", self._on_approve)
        action_box.append(approve_btn)

        needs_work_btn = Gtk.Button(label=_("Needs Work"))
        needs_work_btn.add_css_class("warning")
        needs_work_btn.connect("clicked", self._on_needs_work)
        action_box.append(needs_work_btn)

        reject_btn = Gtk.Button(label=_("Reject"))
        reject_btn.add_css_class("destructive-action")
        reject_btn.connect("clicked", self._on_reject)
        action_box.append(reject_btn)

        clear_btn = Gtk.Button(label=_("Clear"))
        clear_btn.add_css_class("flat")
        clear_btn.connect("clicked", self._on_clear_review)
        action_box.append(clear_btn)

        self._detail_box.append(action_box)

        # Review comment
        comment_label = Gtk.Label(label=_("Review Comment"), xalign=0)
        comment_label.add_css_class("heading")
        self._detail_box.append(comment_label)

        self._comment_entry = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self._comment_entry.set_top_margin(8)
        self._comment_entry.set_bottom_margin(8)
        self._comment_entry.set_left_margin(8)
        comment_scroll = Gtk.ScrolledWindow(min_content_height=60, max_content_height=100)
        comment_scroll.set_child(self._comment_entry)
        self._detail_box.append(comment_scroll)

        save_comment_btn = Gtk.Button(label=_("Save Comment"))
        save_comment_btn.connect("clicked", self._on_save_comment)
        self._detail_box.append(save_comment_btn)

        # Review status indicator
        self._review_indicator = Gtk.Label(label="", xalign=0)
        self._review_indicator.add_css_class("heading")
        self._detail_box.append(self._review_indicator)

        right_box.append(self._empty_status)
        right_box.append(self._detail_box)

        right_scroll = Gtk.ScrolledWindow()
        right_scroll.set_child(right_box)
        paned.set_end_child(right_scroll)
        paned.set_position(380)

        main_box.append(paned)

        # Status bar
        self._status = Gtk.Label(label=_("Ready"), xalign=0)
        self._status.set_margin_start(12)
        self._status.set_margin_end(12)
        self._status.set_margin_top(4)
        self._status.set_margin_bottom(4)
        self._status.add_css_class("dim-label")
        main_box.append(self._status)

        self.set_content(main_box)

        # Welcome
        if not self.settings.get("welcome_shown"):
            GLib.idle_add(self._show_welcome)

    def _show_welcome(self):
        dialog = Adw.Dialog()
        dialog.set_title(_("Welcome"))
        dialog.set_content_width(420)
        dialog.set_content_height(480)

        page = Adw.StatusPage()
        page.set_icon_name("document-edit-symbolic")
        page.set_title(_("Welcome to PO Review Board"))
        page.set_description(_(
            "Review .po translations with ease.\\n\\n"
            "✓ Visual diff between .po file versions\\n"
            "✓ Approve, reject, or mark entries as needs-work\\n"
            "✓ Add review comments per entry\\n"
            "✓ Filter by status, fuzzy, untranslated\\n"
            "✓ Export review reports"
        ))

        btn = Gtk.Button(label=_("Get Started"))
        btn.add_css_class("suggested-action")
        btn.add_css_class("pill")
        btn.set_halign(Gtk.Align.CENTER)
        btn.set_margin_top(12)
        btn.connect("clicked", self._on_welcome_close, dialog)
        page.set_child(btn)

        box = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.set_show_title(False)
        box.add_top_bar(hb)
        box.set_content(page)
        dialog.set_child(box)
        dialog.present(self)

    def _on_welcome_close(self, btn, dialog):
        self.settings["welcome_shown"] = True
        _save_settings(self.settings)
        dialog.close()

    def _on_open(self, btn):
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Open PO File"))
        ff = Gtk.FileFilter()
        ff.set_name(_("PO/POT files"))
        ff.add_pattern("*.po")
        ff.add_pattern("*.pot")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(ff)
        dialog.set_filters(filters)
        dialog.open(self, None, self._on_file_opened)

    def _on_file_opened(self, dialog, result):
        try:
            f = dialog.open_finish(result)
            self._load_po(f.get_path())
        except:
            pass

    def _on_open_diff(self, btn):
        if not self._po_path:
            return
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Open file to compare"))
        ff = Gtk.FileFilter()
        ff.set_name(_("PO/POT files"))
        ff.add_pattern("*.po")
        ff.add_pattern("*.pot")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(ff)
        dialog.set_filters(filters)
        dialog.open(self, None, self._on_diff_opened)

    def _on_diff_opened(self, dialog, result):
        try:
            f = dialog.open_finish(result)
            self._po_path_old = f.get_path()
            with open(self._po_path_old) as fh:
                self._old_entries = {e.msgid: e for e in parse_po(fh.read())}
            self._diff_label.set_visible(True)
            self._diff_scroll.set_visible(True)
            self._status.set_text(_("Diff loaded: %s") % self._po_path_old)
        except:
            pass

    def _load_po(self, path):
        self._po_path = path
        with open(path) as f:
            text = f.read()
        self._entries = parse_po(text)
        self._reviews = _load_reviews(path)
        self._old_entries = {}
        self._po_path_old = None
        self._diff_label.set_visible(False)
        self._diff_scroll.set_visible(False)

        # Apply saved reviews
        for e in self._entries:
            key = e.msgid[:100]
            if key in self._reviews:
                e.review_status = self._reviews[key].get("status", "")
                e.review_comment = self._reviews[key].get("comment", "")

        self._populate_list()
        self._title_widget.set_subtitle(os.path.basename(path))
        self._update_stats()
        self._status.set_text(_("Loaded: %s — %d entries") % (path, len(self._entries)))

    def _populate_list(self):
        while True:
            row = self._entry_list.get_row_at_index(0)
            if row is None:
                break
            self._entry_list.remove(row)

        search = self._search_entry.get_text().lower()

        for i, e in enumerate(self._entries):
            if not self._matches_filter(e):
                continue
            if search and search not in e.msgid.lower() and search not in e.msgstr.lower():
                continue

            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.set_margin_start(8)
            box.set_margin_end(8)
            box.set_margin_top(4)
            box.set_margin_bottom(4)

            # Status indicator
            status_icon = Gtk.Label(label=self._status_icon(e))
            box.append(status_icon)

            # Text
            text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            text_box.set_hexpand(True)
            msgid_label = Gtk.Label(label=e.msgid[:80], xalign=0, ellipsize=Pango.EllipsizeMode.END)
            text_box.append(msgid_label)

            if e.msgstr:
                msgstr_label = Gtk.Label(label=e.msgstr[:60], xalign=0, ellipsize=Pango.EllipsizeMode.END)
                msgstr_label.add_css_class("dim-label")
                msgstr_label.add_css_class("caption")
                text_box.append(msgstr_label)

            box.append(text_box)

            # Flags
            if "fuzzy" in e.flags:
                fuzzy_badge = Gtk.Label(label=_("fuzzy"))
                fuzzy_badge.add_css_class("caption")
                fuzzy_badge.add_css_class("warning")
                box.append(fuzzy_badge)

            row.set_child(box)
            row._entry_index = i
            self._entry_list.append(row)

    def _matches_filter(self, e):
        if self._filter_mode == "all":
            return True
        if self._filter_mode == "untranslated":
            return not e.msgstr
        if self._filter_mode == "fuzzy":
            return "fuzzy" in e.flags
        if self._filter_mode == "approved":
            return e.review_status == "approved"
        if self._filter_mode == "rejected":
            return e.review_status == "rejected"
        if self._filter_mode == "needs-work":
            return e.review_status == "needs-work"
        return True

    def _status_icon(self, e):
        if e.review_status == "approved":
            return "✅"
        if e.review_status == "rejected":
            return "❌"
        if e.review_status == "needs-work":
            return "⚠️"
        if not e.msgstr:
            return "⬜"
        if "fuzzy" in e.flags:
            return "🔶"
        return "🔵"

    def _on_filter_changed(self, dropdown, _):
        modes = ["all", "untranslated", "fuzzy", "approved", "rejected", "needs-work"]
        idx = dropdown.get_selected()
        self._filter_mode = modes[idx] if idx < len(modes) else "all"
        self._populate_list()

    def _on_search_changed(self, entry):
        self._populate_list()

    def _on_entry_selected(self, listbox, row):
        if row is None:
            return
        idx = row._entry_index
        e = self._entries[idx]
        self._current_entry = e

        self._empty_status.set_visible(False)
        self._detail_box.set_visible(True)

        # Context
        ctx = ""
        if e.msgctxt:
            ctx = _("Context: %s") % e.msgctxt
        if e.line_no:
            ctx += f"  (line {e.line_no})" if ctx else f"Line {e.line_no}"
        self._ctx_label.set_text(ctx)

        # Source / translation
        self._msgid_view.get_buffer().set_text(e.msgid)
        self._msgstr_view.get_buffer().set_text(e.msgstr)

        # PO comments
        self._po_comments.set_text("\n".join(e.comments) if e.comments else "")

        # Diff
        if self._old_entries and e.msgid in self._old_entries:
            old = self._old_entries[e.msgid]
            if old.msgstr != e.msgstr:
                diff = list(difflib.unified_diff(
                    old.msgstr.splitlines(), e.msgstr.splitlines(),
                    lineterm="", fromfile=_("old"), tofile=_("new")
                ))
                self._diff_view.get_buffer().set_text("\n".join(diff))
            else:
                self._diff_view.get_buffer().set_text(_("(no changes)"))

        # Review
        self._comment_entry.get_buffer().set_text(e.review_comment or "")
        self._update_review_indicator(e)

    def _update_review_indicator(self, e):
        labels = {
            "approved": "✅ " + _("Approved"),
            "rejected": "❌ " + _("Rejected"),
            "needs-work": "⚠️ " + _("Needs work"),
        }
        self._review_indicator.set_text(labels.get(e.review_status, ""))

    def _set_review(self, status):
        if not hasattr(self, "_current_entry"):
            return
        e = self._current_entry
        e.review_status = status
        key = e.msgid[:100]
        self._reviews[key] = {"status": status, "comment": e.review_comment}
        _save_reviews(self._po_path, self._reviews)
        self._update_review_indicator(e)
        self._update_stats()
        self._populate_list()

    def _on_approve(self, btn):
        self._set_review("approved")

    def _on_needs_work(self, btn):
        self._set_review("needs-work")

    def _on_reject(self, btn):
        self._set_review("rejected")

    def _on_clear_review(self, btn):
        self._set_review("")

    def _on_save_comment(self, btn):
        if not hasattr(self, "_current_entry"):
            return
        buf = self._comment_entry.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        self._current_entry.review_comment = text
        key = self._current_entry.msgid[:100]
        if key not in self._reviews:
            self._reviews[key] = {}
        self._reviews[key]["comment"] = text
        _save_reviews(self._po_path, self._reviews)
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._status.set_text(_("%(time)s — Comment saved") % {"time": ts})

    def _update_stats(self):
        total = len(self._entries)
        translated = sum(1 for e in self._entries if e.msgstr)
        fuzzy = sum(1 for e in self._entries if "fuzzy" in e.flags)
        approved = sum(1 for e in self._entries if e.review_status == "approved")
        self._stats_label.set_text(
            _("%(total)d entries, %(translated)d translated, %(fuzzy)d fuzzy, %(approved)d approved") %
            {"total": total, "translated": translated, "fuzzy": fuzzy, "approved": approved}
        )


# ── Application ──────────────────────────────────────────────

class ReviewApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.window = None

        for name, callback in [
            ("export-report", self._on_export_report),
            ("copy-debug", self._on_copy_debug),
            ("shortcuts", self._on_shortcuts),
            ("about", self._on_about),
            ("quit", self._on_quit),
        ]:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

        self.set_accels_for_action("app.quit", ["<Ctrl>q"])
        self.set_accels_for_action("app.shortcuts", ["<Ctrl>slash"])
        self.set_accels_for_action("app.export-report", ["<Ctrl>e"])

    def do_activate(self):
        if not self.window:
            self.window = ReviewWindow(self)
        self.window.present()

    def _on_export_report(self, *_args):
        if not self.window or not self.window._entries:
            return
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Export Review Report"))
        dialog.set_initial_name(f"review-{datetime.date.today().isoformat()}.json")
        dialog.save(self.window, None, self._on_export_done)

    def _on_export_done(self, dialog, result):
        try:
            f = dialog.save_finish(result)
            path = f.get_path()
            report = {
                "file": self.window._po_path,
                "date": datetime.datetime.now().isoformat(),
                "total": len(self.window._entries),
                "entries": []
            }
            for e in self.window._entries:
                if e.review_status:
                    report["entries"].append({
                        "msgid": e.msgid[:200],
                        "msgstr": e.msgstr[:200],
                        "status": e.review_status,
                        "comment": e.review_comment,
                    })
            with open(path, "w") as fh:
                json.dump(report, fh, indent=2, ensure_ascii=False)
            self.window._status.set_text(_("Report exported to %s") % path)
        except:
            pass

    def _on_copy_debug(self, *_args):
        if not self.window:
            return
        from . import __version__
        info = (
            f"PO Review Board {__version__}\n"
            f"Python {sys.version}\n"
            f"GTK {Gtk.MAJOR_VERSION}.{Gtk.MINOR_VERSION}\n"
            f"Adw {Adw.MAJOR_VERSION}.{Adw.MINOR_VERSION}\n"
            f"OS: {os.uname().sysname} {os.uname().release}\n"
        )
        clipboard = Gdk.Display.get_default().get_clipboard()
        clipboard.set(info)
        self.window._status.set_text(_("Debug info copied"))

    def _on_shortcuts(self, *_args):
        if self.window:
            dialog = Gtk.ShortcutsWindow(transient_for=self.window)
            section = Gtk.ShortcutsSection(visible=True)
            group = Gtk.ShortcutsGroup(title=_("General"), visible=True)
            for accel, title in [
                ("<Ctrl>q", _("Quit")),
                ("<Ctrl>e", _("Export report")),
                ("<Ctrl>slash", _("Keyboard shortcuts")),
            ]:
                group.append(Gtk.ShortcutsShortcut(accelerator=accel, title=title, visible=True))
            section.append(group)
            dialog.append(section)
            dialog.present()

    def _on_about(self, *_args):
        from . import __version__
        dialog = Adw.AboutDialog(
            application_name=_("PO Review Board"),
            application_icon="document-edit-symbolic",
            version=__version__,
            developer_name="Daniel Nylander",
            website="https://github.com/yeager/po-review-board",
            license_type=Gtk.License.GPL_3_0,
            issue_url="https://github.com/yeager/po-review-board/issues",
            comments=_("Review .po translations with diff view, comments and approval workflow."),
        )
        dialog.present(self.window)

    def _on_quit(self, *_args):
        self.quit()


def main():
    app = ReviewApp()
    app.run(sys.argv)


# --- Session restore ---
import json as _json
import os as _os

def _save_session(window, app_name):
    config_dir = _os.path.join(_os.path.expanduser('~'), '.config', app_name)
    _os.makedirs(config_dir, exist_ok=True)
    state = {'width': window.get_width(), 'height': window.get_height(),
             'maximized': window.is_maximized()}
    try:
        with open(_os.path.join(config_dir, 'session.json'), 'w') as f:
            _json.dump(state, f)
    except OSError:
        pass

def _restore_session(window, app_name):
    path = _os.path.join(_os.path.expanduser('~'), '.config', app_name, 'session.json')
    try:
        with open(path) as f:
            state = _json.load(f)
        window.set_default_size(state.get('width', 800), state.get('height', 600))
        if state.get('maximized'):
            window.maximize()
    except (FileNotFoundError, _json.JSONDecodeError, OSError):
        pass


# --- Fullscreen toggle (F11) ---
def _setup_fullscreen(window, app):
    """Add F11 fullscreen toggle."""
    from gi.repository import Gio
    if not app.lookup_action('toggle-fullscreen'):
        action = Gio.SimpleAction.new('toggle-fullscreen', None)
        action.connect('activate', lambda a, p: (
            window.unfullscreen() if window.is_fullscreen() else window.fullscreen()
        ))
        app.add_action(action)
        app.set_accels_for_action('app.toggle-fullscreen', ['F11'])


# --- Plugin system ---
import importlib.util
import os as _pos

def _load_plugins(app_name):
    """Load plugins from ~/.config/<app>/plugins/."""
    plugin_dir = _pos.path.join(_pos.path.expanduser('~'), '.config', app_name, 'plugins')
    plugins = []
    if not _pos.path.isdir(plugin_dir):
        return plugins
    for fname in sorted(_pos.listdir(plugin_dir)):
        if fname.endswith('.py') and not fname.startswith('_'):
            path = _pos.path.join(plugin_dir, fname)
            try:
                spec = importlib.util.spec_from_file_location(fname[:-3], path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                plugins.append(mod)
            except Exception as e:
                print(f"Plugin {fname}: {e}")
    return plugins
