"""E8-011: the time screen as in Harvest, behind Time > Track (design doc 006).

A day with week tabs, Track time (hours on a day, or a timer today), Start and Stop on a row, Edit and
Delete. Only the person's own entries. Writes go through TimeTrackingService.
"""

from datetime import date, datetime, timedelta

import pytest

from app import db
from app.models import Task, TimeEntry
from app.models.time_entry import local_now
from e8.time_screen.service import build_day, fmt_hm, parse_hours

pytestmark = [pytest.mark.integration]

DAY = date(2026, 11, 16)  # a Monday


def _entry(user, project, start, hours, notes=None, task=None):
    e = TimeEntry(
        user_id=user.id,
        project_id=project.id,
        task_id=task.id if task else None,
        start_time=start,
        end_time=start + timedelta(hours=hours),
        notes=notes,
        source="manual",
        billable=True,
    )
    e.calculate_duration()
    db.session.add(e)
    db.session.commit()
    return e


def _day_entries(user, day):
    start = datetime.combine(day, datetime.min.time())
    return (
        TimeEntry.query.filter(
            TimeEntry.user_id == user.id,
            TimeEntry.start_time >= start,
            TimeEntry.start_time < start + timedelta(days=1),
        )
        .order_by(TimeEntry.start_time)
        .all()
    )


# --- Hours as people type them ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, seconds",
    [
        ("1:30", 5400),
        ("1.5", 5400),
        ("1,5", 5400),
        ("90m", 5400),
        ("2h", 7200),
        ("1h 30m", 5400),
        ("1h30", 5400),
        ("0:45", 2700),
        (" 8 ", 8 * 3600),
    ],
)
def test_parse_hours(raw, seconds):
    assert parse_hours(raw) == seconds


@pytest.mark.parametrize("raw", ["", None, "0", "0:00", "abc", "25", "1:75", "-1"])
def test_parse_hours_rejects(raw):
    assert parse_hours(raw) is None


def test_fmt_hm():
    assert fmt_hm(0) == "0:00"
    assert fmt_hm(5400) == "1:30"
    assert fmt_hm(65) == "0:01"


# --- The day and its week --------------------------------------------------------------------------


def test_build_day_counts_tabs_and_lists_the_day(app, user, project):
    _entry(user, project, datetime(2026, 11, 16, 9, 0), 2, notes="Созвон")
    _entry(user, project, datetime(2026, 11, 16, 11, 0), 1)
    _entry(user, project, datetime(2026, 11, 18, 9, 0), 3)
    _entry(user, project, datetime(2026, 11, 9, 9, 0), 5)  # last week, not counted
    view = build_day(user, DAY)
    assert [t.total for t in view.tabs] == ["3:00", "0:00", "3:00", "0:00", "0:00", "0:00", "0:00"]
    assert fmt_hm(view.week_seconds) == "6:00"
    assert [e.notes for e in view.entries] == ["Созвон", None]
    assert view.overwork_level is None


def test_build_day_flags_a_long_day(app, user, project):
    _entry(user, project, datetime(2026, 11, 16, 8, 0), 13)
    assert build_day(user, DAY).overwork_level == "long_day"


def test_page_shows_notes_and_week(app, authenticated_client, user, project):
    _entry(user, project, datetime(2026, 11, 16, 9, 0), 2, notes="Валюта в форме счёта")
    html = authenticated_client.get("/time?date=2026-11-16").get_data(as_text=True)
    assert "Валюта в форме счёта" in html
    assert "Week total" in html
    assert "2:00" in html


def test_page_shows_only_own_entries(app, authenticated_client, user, admin_user, project):
    _entry(admin_user, project, datetime(2026, 11, 16, 9, 0), 2, notes="чужая запись")
    html = authenticated_client.get("/time?date=2026-11-16").get_data(as_text=True)
    assert "чужая запись" not in html


def test_sidebar_track_opens_the_time_screen(app, authenticated_client):
    html = authenticated_client.get("/time").get_data(as_text=True)
    assert 'href="/time"' in html


# --- Track time ------------------------------------------------------------------------------------


def test_hours_on_a_past_day_go_after_the_last_entry(app, authenticated_client, user, project):
    _entry(user, project, datetime(2026, 11, 16, 9, 0), 2)
    resp = authenticated_client.post(
        "/time/api/entries",
        json={"project_id": project.id, "date": "2026-11-16", "hours": "1:30", "notes": "задним числом"},
    )
    assert resp.status_code == 201, resp.get_json()
    entries = _day_entries(user, DAY)
    assert [(e.start_time.hour, e.start_time.minute) for e in entries] == [(9, 0), (11, 0)]
    assert entries[1].duration_seconds == 5400
    assert entries[1].notes == "задним числом"


def test_two_entries_on_the_same_past_day_do_not_clash(app, authenticated_client, user, project):
    for _ in range(2):
        resp = authenticated_client.post(
            "/time/api/entries", json={"project_id": project.id, "date": "2026-11-16", "hours": "2"}
        )
        assert resp.status_code == 201, resp.get_json()
    assert len(_day_entries(user, DAY)) == 2


def test_task_must_belong_to_the_project(app, authenticated_client, user, project, task):
    from app.models import Project

    other = Project(name="Other project", client_id=project.client_id)
    db.session.add(other)
    db.session.commit()
    stray = Task(name="Elsewhere", project_id=other.id, created_by=user.id)
    db.session.add(stray)
    db.session.commit()
    resp = authenticated_client.post(
        "/time/api/entries",
        json={"project_id": project.id, "task_id": stray.id, "date": "2026-11-16", "hours": "1"},
    )
    assert resp.status_code == 400
    resp = authenticated_client.post(
        "/time/api/entries",
        json={"project_id": project.id, "task_id": task.id, "date": "2026-11-16", "hours": "1"},
    )
    assert resp.status_code == 201
    assert _day_entries(user, DAY)[0].task_id == task.id


def test_wrong_hours_are_refused(app, authenticated_client, project):
    resp = authenticated_client.post(
        "/time/api/entries", json={"project_id": project.id, "date": "2026-11-16", "hours": "lots"}
    )
    assert resp.status_code == 400
    assert "1:30" in resp.get_json()["error"]


def test_no_hours_starts_a_timer_today_only(app, authenticated_client, user, project):
    resp = authenticated_client.post("/time/api/entries", json={"project_id": project.id, "date": "2026-11-16"})
    assert resp.status_code == 400
    today = local_now().date().isoformat()
    resp = authenticated_client.post("/time/api/entries", json={"project_id": project.id, "date": today})
    assert resp.status_code == 201, resp.get_json()
    assert TimeEntry.query.filter_by(user_id=user.id, end_time=None).count() == 1


def test_overwork_note_comes_back(app, authenticated_client, user, project):
    _entry(user, project, datetime(2026, 11, 16, 9, 0), 8)
    resp = authenticated_client.post(
        "/time/api/entries", json={"project_id": project.id, "date": "2026-11-16", "hours": "1"}
    )
    assert resp.status_code == 201
    assert resp.get_json()["notice"]["level"] == "overtime"


# --- Edit, delete, Start and Stop ------------------------------------------------------------------


def test_edit_notes_and_hours_in_place(app, authenticated_client, user, project):
    e = _entry(user, project, datetime(2026, 11, 16, 9, 0), 1)
    resp = authenticated_client.patch(f"/time/api/entries/{e.id}", json={"hours": "2:15", "notes": "правка"})
    assert resp.status_code == 200, resp.get_json()
    db.session.refresh(e)
    assert e.start_time == datetime(2026, 11, 16, 9, 0)
    assert e.duration_seconds == 2 * 3600 + 15 * 60
    assert e.notes == "правка"


def test_longer_entry_moves_instead_of_clashing(app, authenticated_client, user, project):
    first = _entry(user, project, datetime(2026, 11, 16, 9, 0), 2)
    _entry(user, project, datetime(2026, 11, 16, 11, 0), 2)
    resp = authenticated_client.patch(f"/time/api/entries/{first.id}", json={"hours": "3"})
    assert resp.status_code == 200, resp.get_json()
    db.session.refresh(first)
    assert first.duration_seconds == 3 * 3600
    assert first.start_time == datetime(2026, 11, 16, 13, 0)  # after the other one


def test_cannot_touch_someone_elses_entry(app, authenticated_client, admin_user, project):
    e = _entry(admin_user, project, datetime(2026, 11, 16, 9, 0), 1)
    assert authenticated_client.patch(f"/time/api/entries/{e.id}", json={"notes": "x"}).status_code == 404
    assert authenticated_client.delete(f"/time/api/entries/{e.id}").status_code == 404
    assert authenticated_client.post(f"/time/api/entries/{e.id}/start").status_code == 404


def test_delete(app, authenticated_client, user, project):
    e = _entry(user, project, datetime(2026, 11, 16, 9, 0), 1)
    assert authenticated_client.delete(f"/time/api/entries/{e.id}").status_code == 200
    assert _day_entries(user, DAY) == []


def test_start_on_a_row_stops_the_running_timer_and_copies_the_row(app, authenticated_client, user, project, task):
    row = _entry(user, project, datetime(2026, 11, 16, 9, 0), 1, notes="та же задача", task=task)
    resp = authenticated_client.post(f"/time/api/entries/{row.id}/start")
    assert resp.status_code == 201, resp.get_json()
    first_timer = TimeEntry.query.filter_by(user_id=user.id, end_time=None).one()
    assert (first_timer.task_id, first_timer.notes) == (task.id, "та же задача")

    resp = authenticated_client.post(f"/time/api/entries/{row.id}/start")
    assert resp.status_code == 201
    assert TimeEntry.query.filter_by(user_id=user.id, end_time=None).count() == 1
    db.session.refresh(first_timer)
    assert first_timer.end_time is not None

    assert authenticated_client.post("/time/api/stop").status_code == 200
    assert TimeEntry.query.filter_by(user_id=user.id, end_time=None).count() == 0


def test_tasks_of_a_project(app, authenticated_client, project, task):
    data = authenticated_client.get(f"/time/api/projects/{project.id}/tasks").get_json()
    assert [t["name"] for t in data["tasks"]] == [task.name]


# --- Module switch ---------------------------------------------------------------------------------


def test_module_off_hides_the_screen_and_track_goes_to_dashboard(app, authenticated_client):
    from app.models import Settings

    settings = Settings.get_settings()
    settings.disabled_module_ids = ["e8_time"]
    db.session.commit()
    assert authenticated_client.get("/time").status_code in (302, 403, 404)
    html = authenticated_client.get("/dashboard").get_data(as_text=True)
    assert 'href="/time"' not in html
