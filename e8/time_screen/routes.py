"""Routes of the time screen. Pages: GET /time. JSON under /time/api, used by the page's script.

Every write goes through TimeTrackingService. After a write the page reloads, so the overwork note
(E8-016) goes out as a flash message, the same way as on the dashboard.
"""

from datetime import date, datetime, time, timedelta

from flask import abort, current_app, flash, jsonify, render_template, request, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required

from app import db
from app.models import Task, TimeEntry
from app.models.time_entry import local_now
from app.services.time_tracking_service import TimeTrackingService
from app.utils.module_helpers import module_enabled
from app.utils.scope_filter import get_active_projects_for_user, user_can_access_project
from e8.time_screen import MODULE_ID, e8_time_bp
from e8.time_screen.service import build_day, clashes, fmt_hm, parse_hours


def _parse_day(raw) -> date:
    try:
        return date.fromisoformat(str(raw)[:10]) if raw else local_now().date()
    except ValueError:
        return local_now().date()


def _projects_for(user):
    """Projects the person may log to, grouped by client in the dialog."""
    projects = get_active_projects_for_user(user)
    return sorted(projects, key=lambda p: ((p.client or "").lower(), (p.name or "").lower()))


def _error(message, status=400):
    return jsonify({"success": False, "error": message}), status


def _own_entry(entry_id) -> TimeEntry:
    entry = db.session.get(TimeEntry, entry_id)
    if entry is None or entry.user_id != current_user.id:
        abort(404)
    return entry


def _after_write(day: date):
    """Drop the dashboard cache and flash the overwork note for the day, if any."""
    try:
        from app.utils.cache import invalidate_dashboard_for_user

        invalidate_dashboard_for_user(current_user.id)
    except Exception:
        current_app.logger.debug("Dashboard cache not dropped", exc_info=True)
    try:
        from app.utils.overwork_notes import note_for_day

        note = note_for_day(current_user, day)
        if note:
            flash(note["text"], "warning" if note["level"] == "long_day" else "info")
            return note
    except Exception:
        current_app.logger.debug("Overwork note skipped", exc_info=True)
    return None


def _task_id_from(data, project_id):
    """Task from the request, checked to belong to the project. 0 means "no task"."""
    raw = data.get("task_id")
    if raw in (None, "", 0, "0"):
        return 0
    task = db.session.get(Task, int(raw))
    if task is None or task.project_id != int(project_id):
        return None
    return task.id


# --- Page -----------------------------------------------------------------------------------------


@e8_time_bp.route("/")  # applied first, so url_for gives /time
@e8_time_bp.route("")
@login_required
@module_enabled(MODULE_ID)
def day():
    view = build_day(current_user, _parse_day(request.args.get("date")))
    return render_template(
        "e8_time/day.html",
        view=view,
        projects=_projects_for(current_user),
        fmt_hm=fmt_hm,
        prev_day=view.day - timedelta(days=1),
        next_day=view.day + timedelta(days=1),
        prev_week=view.day - timedelta(days=7),
        next_week=view.day + timedelta(days=7),
    )


# --- JSON -----------------------------------------------------------------------------------------


@e8_time_bp.route("/api/projects/<int:project_id>/tasks")
@login_required
@module_enabled(MODULE_ID)
def project_tasks(project_id):
    if not user_can_access_project(current_user, project_id):
        return _error(_("You do not have access to this project"), 403)
    tasks = Task.query.filter_by(project_id=project_id).order_by(Task.name).all()
    closed = {"done", "cancelled"}
    tasks.sort(key=lambda t: (t.status in closed, (t.name or "").lower()))
    return jsonify({"tasks": [{"id": t.id, "name": t.name, "closed": t.status in closed} for t in tasks]})


@e8_time_bp.route("/api/entries", methods=["POST"])
@login_required
@module_enabled(MODULE_ID)
def create_entry():
    """Save hours on a day, or start a timer when no hours are given (today only)."""
    data = request.get_json(silent=True) or {}
    project_id = data.get("project_id")
    if not project_id:
        return _error(_("Choose a project"))
    if not user_can_access_project(current_user, project_id):
        return _error(_("You do not have access to this project"), 403)
    task_id = _task_id_from(data, project_id)
    if task_id is None:
        return _error(_("This task is not in the chosen project"))
    notes = (data.get("notes") or "").strip() or None
    day = _parse_day(data.get("date"))
    service = TimeTrackingService()

    hours_raw = (data.get("hours") or "").strip()
    if not hours_raw:
        if day != local_now().date():
            return _error(_("Enter hours for this day. A timer can only run today."))
        if current_user.active_timer:
            stopped = service.stop_timer(user_id=current_user.id, entry_id=current_user.active_timer.id)
            if not stopped.get("success"):
                return _error(stopped.get("message") or _("Could not stop the running timer"))
        result = service.start_timer(
            user_id=current_user.id, project_id=int(project_id), task_id=task_id or None, notes=notes
        )
        if not result.get("success"):
            return _error(result.get("message") or _("Could not start the timer"))
        _after_write(day)
        return jsonify({"success": True, "entry_id": result["timer"].id, "running": True}), 201

    seconds = parse_hours(hours_raw)
    if seconds is None:
        return _error(_("Enter hours as 1:30, 1.5 or 90m"))
    placement = service.place_duration_entry(current_user.id, day, seconds)
    if not placement.get("success"):
        return _error(placement.get("message") or _("No room left on this day"))
    result = service.create_manual_entry(
        user_id=current_user.id,
        project_id=int(project_id),
        task_id=task_id or None,
        start_time=placement["start_time"],
        end_time=placement["end_time"],
        notes=notes,
    )
    if not result.get("success"):
        return _error(result.get("message") or _("Could not save the entry"))
    note = _after_write(day)
    return jsonify({"success": True, "entry_id": result["entry"].id, "notice": note}), 201


@e8_time_bp.route("/api/entries/<int:entry_id>", methods=["PATCH"])
@login_required
@module_enabled(MODULE_ID)
def update_entry(entry_id):
    """Change project, task, notes and hours. A longer entry that would run into the next one moves to a
    free place on the same day, the way new hours are placed (E8-012)."""
    entry = _own_entry(entry_id)
    data = request.get_json(silent=True) or {}
    service = TimeTrackingService()
    kwargs = {}

    project_id = data.get("project_id") or entry.project_id
    if not project_id:
        return _error(_("Choose a project"))
    if int(project_id) != entry.project_id:
        if not user_can_access_project(current_user, project_id):
            return _error(_("You do not have access to this project"), 403)
        kwargs["project_id"] = int(project_id)
    if "task_id" in data:
        task_id = _task_id_from(data, project_id)
        if task_id is None:
            return _error(_("This task is not in the chosen project"))
        kwargs["task_id"] = task_id
    if "notes" in data:
        kwargs["notes"] = (data.get("notes") or "").strip()

    hours_raw = (data.get("hours") or "").strip()
    if hours_raw and entry.end_time is not None:
        seconds = parse_hours(hours_raw)
        if seconds is None:
            return _error(_("Enter hours as 1:30, 1.5 or 90m"))
        if seconds != (entry.duration_seconds or 0):
            start = entry.start_time
            end = start + timedelta(seconds=seconds)
            midnight = datetime.combine(start.date() + timedelta(days=1), time(0, 0))
            if end > midnight or clashes(current_user.id, start, end, exclude_entry_id=entry.id):
                placement = service.place_duration_entry(
                    current_user.id, start.date(), seconds, exclude_entry_id=entry.id
                )
                if not placement.get("success"):
                    return _error(placement.get("message") or _("No room left on this day"))
                start, end = placement["start_time"], placement["end_time"]
            kwargs["start_time"], kwargs["end_time"] = start, end

    if not kwargs:
        return jsonify({"success": True, "entry_id": entry.id})
    result = service.update_entry(entry_id=entry.id, user_id=current_user.id, **kwargs)
    if not result.get("success"):
        return _error(result.get("message") or _("Could not save the entry"))
    note = _after_write(entry.start_time.date())
    return jsonify({"success": True, "entry_id": entry.id, "notice": note})


@e8_time_bp.route("/api/entries/<int:entry_id>", methods=["DELETE"])
@login_required
@module_enabled(MODULE_ID)
def delete_entry(entry_id):
    entry = _own_entry(entry_id)
    day = entry.start_time.date()
    result = TimeTrackingService().delete_entry(user_id=current_user.id, entry_id=entry.id)
    if not result.get("success"):
        return _error(result.get("message") or _("Could not delete the entry"))
    _after_write(day)
    return jsonify({"success": True})


@e8_time_bp.route("/api/entries/<int:entry_id>/start", methods=["POST"])
@login_required
@module_enabled(MODULE_ID)
def start_from_entry(entry_id):
    """Start on a row: a new timer today with the row's project, task and notes (design doc 006, 3.3).
    A timer that is already running stops first, as in Harvest."""
    entry = _own_entry(entry_id)
    if not entry.project_id:
        return _error(_("This entry has no project"))
    service = TimeTrackingService()
    active = current_user.active_timer
    if active:
        stopped = service.stop_timer(user_id=current_user.id, entry_id=active.id)
        if not stopped.get("success"):
            return _error(stopped.get("message") or _("Could not stop the running timer"))
    result = service.start_timer(
        user_id=current_user.id, project_id=entry.project_id, task_id=entry.task_id, notes=entry.notes
    )
    if not result.get("success"):
        return _error(result.get("message") or _("Could not start the timer"))
    _after_write(local_now().date())
    return jsonify({"success": True, "entry_id": result["timer"].id, "redirect": url_for("e8_time.day")}), 201


@e8_time_bp.route("/api/stop", methods=["POST"])
@login_required
@module_enabled(MODULE_ID)
def stop():
    active = current_user.active_timer
    if not active:
        return _error(_("No timer is running"))
    day = active.start_time.date()
    result = TimeTrackingService().stop_timer(user_id=current_user.id, entry_id=active.id)
    if not result.get("success"):
        return _error(result.get("message") or _("Could not stop the timer"))
    _after_write(day)
    return jsonify({"success": True})
